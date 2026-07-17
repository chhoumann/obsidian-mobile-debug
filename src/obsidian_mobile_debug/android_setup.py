"""Provision a disposable Android Obsidian runtime, including no-KVM hosts.

The Android CLI transport intentionally knows nothing about emulator lifecycle.
This module owns that boundary: official SDK installation, isolated AVD state,
software-emulator bootstrap, APK verification, and boot-health assertions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


COMMANDLINE_TOOLS_REVISION = "14742923"
COMMANDLINE_TOOLS: dict[str, tuple[str, str]] = {
    "Linux": (
        f"https://dl.google.com/android/repository/commandlinetools-linux-"
        f"{COMMANDLINE_TOOLS_REVISION}_latest.zip",
        "48833c34b761c10cb20bcd16582129395d121b27",
    ),
    "Darwin": (
        f"https://dl.google.com/android/repository/commandlinetools-mac-"
        f"{COMMANDLINE_TOOLS_REVISION}_latest.zip",
        "cc27cca4b84bfdbc7df17e3d0a01d0c640d8ee71",
    ),
}
OBSIDIAN_RELEASE_API = "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"
OBSIDIAN_SIGNER_SHA256 = "bd3bd52f4427b5ab7f8059d071a35e3de943c646546d32313da5f1cac254b7e4"
WATCHDOG_MARKERS = (
    "WATCHDOG KILLING SYSTEM PROCESS",
    "*** GOODBYE!",
)
APP_CRASH_MARKERS = (
    "crash wasn't handled by all associated  webviews",
    "Renderer process (",
    "FATAL EXCEPTION: main",
)
SOFTWARE_WEBVIEW_FLAGS = (
    "--disable-hang-monitor",
    "--webview-verbose-logging",
)
# Preserve QEMU's broad software CPU model and boot-critical SSSE3 support.
# Masking SSSE3 makes current Android images unable to expose ADB in a practical
# time, while narrower SIMD and virtual-clock experiments did not prevent the
# modern Google WebView PNG decoder from trapping under TCG. The CDP stability
# gate therefore remains authoritative; no-KVM Linux hosts should use ReDroid.
SOFTWARE_CPU = "max"
WEBVIEW_COMMAND_LINE = "/data/local/tmp/webview-command-line"
REDROID_IMAGE_DIGEST = "sha256:0a611199ba2e0b5d60af39b3327a517f6407231f4352114ed3bd3cbfe2be69aa"
REDROID_IMAGE = f"redroid/redroid@{REDROID_IMAGE_DIGEST}"
REDROID_BINDER_DEVICE = Path("/dev/binderfs/binder")
ALLOWED_DOWNLOAD_HOSTS = {
    "dl.google.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class SetupError(RuntimeError):
    """A setup failure with a user-actionable message."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    sha256: str
    version: str


@dataclass(frozen=True)
class SetupConfig:
    backend: str
    api: int
    abi: str
    avd: str
    device: str
    bundle: str
    gpu: str
    sdk_root: Path
    android_home: Path
    state_dir: Path
    apk: Path | None
    acceleration: str
    adb_timeout: float
    boot_timeout: float
    console_port: int
    adb_port: int
    reset: bool
    timeout_multiplier: int
    signer_sha256: str
    acknowledge_privileged_container: bool

    @property
    def serial(self) -> str:
        if self.backend == "container":
            return f"127.0.0.1:{self.adb_port}"
        return f"emulator-{self.console_port}"

    @property
    def container_name(self) -> str:
        return f"omd-{self.avd}-redroid"

    @property
    def container_data(self) -> Path:
        return (self.state_dir / "redroid" / self.avd / "data").resolve()

    @property
    def image(self) -> str:
        return f"system-images;android-{self.api};google_apis;{self.abi}"

    @property
    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "ANDROID_SDK_ROOT": str(self.sdk_root),
            "ANDROID_HOME": str(self.sdk_root),
            "ANDROID_USER_HOME": str(self.android_home),
            "ANDROID_AVD_HOME": str(self.android_home / "avd"),
        })
        return env


def _default_state_dir(environ: Mapping[str, str] = os.environ) -> Path:
    cache = Path(environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "obsidian-mobile-debug"


def default_sdk_root(environ: Mapping[str, str] = os.environ) -> Path:
    if environ.get("ANDROID_SDK_ROOT"):
        return Path(environ["ANDROID_SDK_ROOT"]).expanduser()
    homebrew = Path("/opt/homebrew/share/android-commandlinetools")
    if platform.system() == "Darwin" and homebrew.exists():
        return homebrew
    return _default_state_dir(environ) / "android-sdk"


def config_from_args(args: Any) -> SetupConfig:
    backend = getattr(args, "backend", "emulator")
    if backend not in {"emulator", "container"}:
        raise SetupError("--backend must be emulator or container")
    if args.api < 1:
        raise SetupError("--api must be a positive integer")
    if args.console_port < 5554 or args.console_port > 5682 or args.console_port % 2:
        raise SetupError("--console-port must be an even number from 5554 through 5682")
    adb_port = getattr(args, "adb_port", 5555)
    if adb_port < 1024 or adb_port > 65535:
        raise SetupError("--adb-port must be from 1024 through 65535")
    if args.timeout_multiplier < 1:
        raise SetupError("--timeout-multiplier must be a positive integer")
    if args.adb_timeout <= 0 or args.boot_timeout <= 0:
        raise SetupError("--adb-timeout and --boot-timeout must be positive")
    signer_sha256 = args.signer_sha256.lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{64}", signer_sha256):
        raise SetupError("--signer-sha256 must contain exactly 64 hexadecimal characters")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", args.bundle):
        raise SetupError(f"Invalid Android application id: {args.bundle!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,42}", args.avd):
        raise SetupError("--avd must be 1-43 safe container/AVD name characters")
    acknowledge_container = getattr(args, "acknowledge_privileged_container", False)
    if backend == "container":
        if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
            raise SetupError("The pinned ReDroid backend currently supports Linux x86_64 only")
        if args.api != 34:
            raise SetupError("The pinned ReDroid backend provides Android API 34; use --api 34")
        if not acknowledge_container:
            raise SetupError(
                "The container backend runs a digest-pinned ReDroid image with --privileged and "
                "SELinux unavailable. Re-run with --acknowledge-privileged-container only on an "
                "isolated test host. ADB remains bound to 127.0.0.1."
            )

    state_dir = Path(args.state_dir).expanduser() if args.state_dir else _default_state_dir()
    sdk_root = Path(args.sdk_root).expanduser() if args.sdk_root else default_sdk_root()
    android_home = (
        Path(args.android_home).expanduser()
        if args.android_home
        else state_dir / "android-user"
    )
    abi = args.abi
    if abi == "auto":
        abi = "arm64-v8a" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    return SetupConfig(
        backend=backend,
        api=args.api,
        abi=abi,
        avd=args.avd,
        device=args.device,
        bundle=args.bundle,
        gpu=args.gpu,
        sdk_root=sdk_root,
        android_home=android_home,
        state_dir=state_dir,
        apk=Path(args.apk).expanduser().resolve() if args.apk else None,
        acceleration=args.acceleration,
        adb_timeout=args.adb_timeout,
        boot_timeout=args.boot_timeout,
        console_port=args.console_port,
        adb_port=adb_port,
        reset=args.reset,
        timeout_multiplier=args.timeout_multiplier,
        signer_sha256=signer_sha256,
        acknowledge_privileged_container=acknowledge_container,
    )


def run(
    command: Sequence[str | Path], *, env: Mapping[str, str] | None = None,
    check: bool = True, timeout: float | None = None, input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    rendered = [str(part) for part in command]
    try:
        return subprocess.run(
            rendered,
            env=env,
            check=check,
            timeout=timeout,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise SetupError(f"Required executable not found: {rendered[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"Timed out after {timeout}s: {' '.join(rendered)}") from exc
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "").strip()
        raise SetupError(f"Command failed ({exc.returncode}): {' '.join(rendered)}\n{output}") from exc


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise SetupError(f"Refusing untrusted Android setup download URL: {url!r}")


def _download(url: str, destination: Path) -> None:
    _validate_download_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "obsidian-mobile-debug"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        # URL and redirect destination are constrained to the HTTPS allowlist.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=60
        ) as response, temporary.open("wb") as output:
            _validate_download_url(response.geturl())
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _commandline_tools_spec() -> tuple[str, str]:
    system = platform.system()
    try:
        return COMMANDLINE_TOOLS[system]
    except KeyError as exc:
        raise SetupError(f"Automatic Android SDK installation is not supported on {system}") from exc


def ensure_commandline_tools(config: SetupConfig) -> Path:
    sdkmanager = config.sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if sdkmanager.is_file():
        return sdkmanager
    if not shutil.which("java"):
        raise SetupError(
            "Java 21+ is required. Install a JRE first "
            "(Ubuntu: sudo apt-get install openjdk-21-jre-headless)."
        )

    url, expected_sha1 = _commandline_tools_spec()
    archive = config.state_dir / "downloads" / Path(url).name
    if not archive.exists() or _hash_file(archive, "sha1") != expected_sha1:
        _download(url, archive)
    actual_sha1 = _hash_file(archive, "sha1")
    if actual_sha1 != expected_sha1:
        raise SetupError(
            f"Android command-line tools checksum mismatch: expected {expected_sha1}, got {actual_sha1}"
        )

    target = config.sdk_root / "cmdline-tools" / "latest"
    temporary = config.state_dir / "commandline-tools-extract"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        root = temporary.resolve()
        for member in package.infolist():
            destination = (temporary / member.filename).resolve()
            if not destination.is_relative_to(root):
                raise SetupError(
                    f"Android command-line tools archive contains an unsafe path: {member.filename!r}"
                )
        package.extractall(temporary)
    extracted = temporary / "cmdline-tools"
    if not (extracted / "bin" / "sdkmanager").is_file():
        raise SetupError("Downloaded Android command-line tools archive has an unexpected layout")
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(extracted), str(target))
    shutil.rmtree(temporary, ignore_errors=True)
    return sdkmanager


def ensure_sdk(config: SetupConfig) -> dict[str, Path]:
    sdkmanager = ensure_commandline_tools(config)
    packages = [
        "platform-tools",
        f"platforms;android-{config.api}",
        f"build-tools;{config.api}.0.0",
    ]
    if config.backend == "emulator":
        packages.extend(["emulator", config.image])
    licenses = run(
        [sdkmanager, f"--sdk_root={config.sdk_root}", "--licenses"],
        env=config.env,
        check=False,
        timeout=180,
        input_text="y\n" * 200,
    )
    if licenses.returncode not in {0, 141}:
        raise SetupError(f"Android SDK license acceptance failed:\n{licenses.stdout.strip()}")
    run(
        [sdkmanager, f"--sdk_root={config.sdk_root}", *packages],
        env=config.env,
        timeout=1800,
    )
    tools = {
        "sdkmanager": sdkmanager,
        "avdmanager": sdkmanager.with_name("avdmanager"),
        "adb": config.sdk_root / "platform-tools" / "adb",
        "apksigner": config.sdk_root / "build-tools" / f"{config.api}.0.0" / "apksigner",
    }
    if config.backend == "emulator":
        tools["emulator"] = config.sdk_root / "emulator" / "emulator"
    missing = [str(path) for path in tools.values() if not path.is_file()]
    if missing:
        raise SetupError(f"Android SDK installation is incomplete: {missing}")
    return tools


def ensure_avd(config: SetupConfig, avdmanager: Path, emulator: Path) -> None:
    config.android_home.mkdir(parents=True, exist_ok=True)
    (config.android_home / "avd").mkdir(parents=True, exist_ok=True)
    avds = run([emulator, "-list-avds"], env=config.env, timeout=30).stdout.splitlines()
    if config.avd in {name.strip() for name in avds} and not config.reset:
        return
    if config.avd in {name.strip() for name in avds}:
        run([avdmanager, "delete", "avd", "--name", config.avd], env=config.env, timeout=60)
    run(
        [
            avdmanager, "create", "avd", "--force", "--name", config.avd,
            "--package", config.image, "--device", config.device,
        ],
        env=config.env,
        timeout=120,
        input_text="no\n",
    )


def docker_prefix() -> list[str]:
    """Return a non-interactive Docker command, preferring unprivileged access."""
    docker = shutil.which("docker")
    if not docker:
        raise SetupError("Docker is required for --backend container")
    direct = run([docker, "info"], check=False, timeout=30)
    if direct.returncode == 0:
        return [docker]
    sudo = shutil.which("sudo")
    if sudo:
        elevated = run([sudo, "-n", docker, "info"], check=False, timeout=30)
        if elevated.returncode == 0:
            return [sudo, "-n", docker]
    raise SetupError(
        "Docker is installed but inaccessible. Add this user to the docker group or configure "
        "non-interactive sudo for Docker; OMD never prompts for elevation."
    )


def _remove_container_data(config: SetupConfig) -> None:
    root = (config.state_dir / "redroid").resolve()
    target = config.container_data.resolve()
    if not target.is_relative_to(root) or target == root:
        raise SetupError(f"Refusing to remove container data outside OMD state: {target}")
    try:
        shutil.rmtree(target, ignore_errors=False)
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        raise SetupError(
            f"Cannot reset root-owned ReDroid data at {target}; remove it with sudo, then retry"
        ) from exc


def ensure_redroid(config: SetupConfig) -> tuple[list[str], bool, dict[str, Any]]:
    """Start or validate the digest-pinned, loopback-only ReDroid container."""
    if not REDROID_BINDER_DEVICE.exists():
        raise SetupError(
            "BinderFS is required for ReDroid. On Ubuntu install the matching "
            "linux-modules-extra package, load binder_linux, and mount binderfs at /dev/binderfs."
        )
    docker = docker_prefix()
    run([*docker, "pull", REDROID_IMAGE], timeout=1800)
    image = run(
        [*docker, "image", "inspect", REDROID_IMAGE, "--format", "{{.Id}} {{.Architecture}}"],
        timeout=60,
    ).stdout.strip().split()
    if image != [REDROID_IMAGE_DIGEST, "amd64"]:
        raise SetupError(
            f"Pinned ReDroid image identity mismatch: expected {[REDROID_IMAGE_DIGEST, 'amd64']}, "
            f"got {image}"
        )

    redroid_root = (config.state_dir / "redroid").resolve()
    instance_root = config.container_data.parent
    redroid_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    instance_root.mkdir(mode=0o700, exist_ok=True)
    redroid_root.chmod(0o700)
    instance_root.chmod(0o700)

    inspect = run(
        [*docker, "inspect", config.container_name, "--format", "{{.State.Running}} {{.Config.Image}}"],
        check=False,
        timeout=30,
    )
    if config.reset and inspect.returncode == 0:
        run([*docker, "rm", "-f", config.container_name], timeout=60)
        inspect = run(
            [*docker, "inspect", config.container_name], check=False, timeout=30
        )
    if config.reset:
        _remove_container_data(config)

    launched = False
    if inspect.returncode == 0:
        state = inspect.stdout.strip().split(maxsplit=1)
        if state != ["true", REDROID_IMAGE]:
            raise SetupError(
                f"Existing container {config.container_name!r} has unexpected state/image: {state}"
            )
    else:
        config.container_data.mkdir(parents=True, mode=0o700, exist_ok=True)
        config.container_data.chmod(0o700)
        run(
            [
                *docker, "run", "-d", "--rm", "--privileged",
                "--name", config.container_name,
                "--mount", f"type=bind,src={config.container_data},dst=/data",
                "--publish", f"127.0.0.1:{config.adb_port}:5555",
                REDROID_IMAGE,
                "androidboot.redroid_gpu_mode=guest",
                "androidboot.use_memfd=true",
            ],
            timeout=180,
        )
        launched = True

    identity_template = (
        "{{.State.Running}}\n{{.Config.Image}}\n{{.HostConfig.Privileged}}\n"
        "{{range .Mounts}}{{if eq .Destination \"/data\"}}{{.Source}}{{end}}{{end}}"
    )
    identity = run(
        [*docker, "inspect", config.container_name, "--format", identity_template], timeout=30
    ).stdout.strip().splitlines()
    expected_identity = ["true", REDROID_IMAGE, "true", str(config.container_data.resolve())]
    if identity != expected_identity:
        if launched:
            run([*docker, "rm", "-f", config.container_name], check=False, timeout=60)
        raise SetupError(
            f"Refusing ReDroid container with unexpected runtime identity: {identity}; "
            f"expected {expected_identity}"
        )

    binding = run(
        [*docker, "port", config.container_name, "5555/tcp"], timeout=30
    ).stdout.strip()
    expected_binding = f"127.0.0.1:{config.adb_port}"
    if binding != expected_binding:
        if launched:
            run([*docker, "rm", "-f", config.container_name], check=False, timeout=60)
        raise SetupError(
            f"Refusing non-loopback or unexpected ReDroid ADB binding: {binding!r}; "
            f"expected {expected_binding!r}"
        )
    return docker, launched, {
        "name": config.container_name,
        "image": REDROID_IMAGE,
        "imageDigest": REDROID_IMAGE_DIGEST,
        "adbBinding": binding,
        "data": str(config.container_data),
        "privileged": True,
    }


def choose_acceleration(requested: str, emulator: Path, env: Mapping[str, str]) -> str:
    if requested in {"on", "off"}:
        return requested
    check = run([emulator, "-accel-check"], env=env, check=False, timeout=30)
    return "on" if check.returncode == 0 else "off"


def emulator_command(config: SetupConfig, emulator: Path, acceleration: str) -> list[str]:
    gpu = "software" if config.gpu == "auto" and acceleration == "off" else config.gpu
    cores = min(os.cpu_count() or 2, 4)
    command = [
        str(emulator), "-avd", config.avd,
        "-port", str(config.console_port),
        "-accel", acceleration,
        "-no-window", "-no-audio", "-no-boot-anim",
        "-gpu", gpu,
        "-memory", "4096", "-cores", str(cores),
        "-no-metrics", "-no-sim",
    ]
    if config.reset:
        command.append("-wipe-data")
    if acceleration == "off":
        command.extend([
            "-no-snapshot", "-skin", "480x800", "-selinux", "permissive",
            "-qemu", "-cpu", SOFTWARE_CPU,
        ])
    return command


def _adb(
    adb: Path, serial: str, *args: str, check: bool = True, timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return run([adb, "-s", serial, *args], check=check, timeout=timeout)


def _adb_text(
    adb: Path, serial: str, *args: str, check: bool = True, timeout: float = 120,
) -> str:
    return _adb(adb, serial, *args, check=check, timeout=timeout).stdout.strip().replace("\r", "")


def wait_for_adb(adb: Path, serial: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _adb_text(adb, serial, "get-state", check=False, timeout=10)
        if state == "device":
            return
        time.sleep(5)
    raise SetupError(f"{serial} did not become available through ADB within {timeout:.0f}s")


def connect_container_adb(config: SetupConfig, adb: Path) -> None:
    deadline = time.monotonic() + config.adb_timeout
    while time.monotonic() < deadline:
        _adb(adb, config.serial, "reconnect", check=False, timeout=10)
        run([adb, "connect", config.serial], check=False, timeout=10)
        if _adb_text(adb, config.serial, "get-state", check=False, timeout=10) == "device":
            return
        time.sleep(2)
    raise SetupError(
        f"Digest-pinned ReDroid did not become available at {config.serial} within "
        f"{config.adb_timeout:.0f}s"
    )


def bootstrap_software_emulator(config: SetupConfig, adb: Path) -> None:
    wait_for_adb(adb, config.serial, config.adb_timeout)
    root = _adb_text(adb, config.serial, "root", check=False, timeout=120)
    if "cannot run as root" in root.lower():
        raise SetupError("Software bootstrap requires an Android userdebug image with adb root support")
    wait_for_adb(adb, config.serial, config.adb_timeout)

    _adb(adb, config.serial, "shell", "setenforce", "0", timeout=30)
    _adb(
        adb, config.serial, "shell", "setprop", "ro.hw_timeout_multiplier",
        str(config.timeout_multiplier), timeout=30,
    )
    multiplier = _adb_text(adb, config.serial, "shell", "getprop", "ro.hw_timeout_multiplier")
    if multiplier != str(config.timeout_multiplier):
        raise SetupError(
            f"Failed to set ro.hw_timeout_multiplier: expected {config.timeout_multiplier}, got {multiplier!r}"
        )
    _adb(adb, config.serial, "shell", "setenforce", "1", timeout=30)
    enforcing = _adb_text(adb, config.serial, "shell", "getenforce")
    if enforcing != "Enforcing":
        raise SetupError(f"Refusing to continue while SELinux is {enforcing or 'unknown'}")

    # ADB is available before Android finishes activating its APEX modules. Do
    # not call `stop` here unless system_server was already running: interrupting
    # init that early can leave ART dependencies unmounted. On TCG the property
    # is normally installed minutes before system_server starts, so init can
    # continue naturally. A faster host may already have started system_server;
    # restart services there so the process observes the new multiplier.
    system_server = _adb_text(
        adb, config.serial, "shell", "pidof", "system_server", check=False, timeout=30
    )
    restart_services = bool(system_server)
    if restart_services:
        _adb(adb, config.serial, "shell", "stop", timeout=120)
    _adb(adb, config.serial, "logcat", "-c", check=False, timeout=30)
    if restart_services:
        _adb(adb, config.serial, "shell", "start", timeout=120)


def boot_state(config: SetupConfig, adb: Path, acceleration: str) -> dict[str, str]:
    state = {
        "adb": _adb_text(adb, config.serial, "get-state", check=False, timeout=10),
        "bootCompleted": _adb_text(
            adb, config.serial, "shell", "getprop", "sys.boot_completed", check=False, timeout=10
        ),
        "systemServer": _adb_text(
            adb, config.serial, "shell", "pidof", "system_server", check=False, timeout=10
        ),
        "packageService": _adb_text(
            adb, config.serial, "shell", "service", "check", "package", check=False, timeout=10
        ),
        "selinux": _adb_text(adb, config.serial, "shell", "getenforce", check=False, timeout=10),
        "timeoutMultiplier": _adb_text(
            adb, config.serial, "shell", "getprop", "ro.hw_timeout_multiplier",
            check=False, timeout=10,
        ),
    }
    state["ready"] = str(
        state["adb"] == "device"
        and state["bootCompleted"] == "1"
        and bool(state["systemServer"])
        and state["packageService"] == "Service package: found"
        and state["selinux"] == ("Disabled" if config.backend == "container" else "Enforcing")
        and (
            config.backend == "container"
            or acceleration != "off"
            or state["timeoutMultiplier"] == str(config.timeout_multiplier)
        )
    ).lower()
    return state


def wait_for_stable_boot(config: SetupConfig, adb: Path, acceleration: str) -> dict[str, str]:
    deadline = time.monotonic() + config.boot_timeout
    last: dict[str, str] = {}
    while time.monotonic() < deadline:
        last = boot_state(config, adb, acceleration)
        print(
            f"boot={last['bootCompleted'] or '-'} server={last['systemServer'] or '-'} "
            f"package={last['packageService'] or '-'} selinux={last['selinux'] or '-'}",
            file=sys.stderr,
            flush=True,
        )
        if last["ready"] == "true":
            first_pid = last["systemServer"]
            time.sleep(30)
            stable = boot_state(config, adb, acceleration)
            if stable["ready"] != "true" or stable["systemServer"] != first_pid:
                raise SetupError(
                    "Android reached boot_completed but did not remain stable for 30 seconds: "
                    f"before={last}, after={stable}"
                )
            logs = _adb_text(adb, config.serial, "logcat", "-d", "-v", "brief", check=False, timeout=120)
            marker = next((value for value in WATCHDOG_MARKERS if value in logs), None)
            if marker:
                raise SetupError(f"Android watchdog failure detected after bootstrap: {marker}")
            return stable
        time.sleep(15)
    raise SetupError(f"Android did not reach a stable boot within {config.boot_timeout:.0f}s: {last}")


def select_release_asset(payload: Mapping[str, Any]) -> ReleaseAsset:
    candidates = [asset for asset in payload.get("assets", []) if asset.get("name", "").endswith(".apk")]
    if len(candidates) != 1:
        raise SetupError(f"Expected one APK in the latest Obsidian release, found {len(candidates)}")
    asset = candidates[0]
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or not re.fullmatch(r"[0-9a-fA-F]{64}", digest[7:]):
        raise SetupError("The latest Obsidian APK has no valid GitHub SHA-256 digest")
    return ReleaseAsset(
        name=asset["name"],
        url=asset["browser_download_url"],
        sha256=digest[7:].lower(),
        version=str(payload.get("tag_name") or "unknown"),
    )


def latest_obsidian_asset() -> ReleaseAsset:
    request = urllib.request.Request(
        OBSIDIAN_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "obsidian-mobile-debug"},
    )
    # This request is fixed to the official GitHub HTTPS API endpoint.
    with urllib.request.urlopen(  # nosec B310
        request, timeout=30
    ) as response:
        return select_release_asset(json.load(response))


def verify_apk_signature(apksigner: Path, apk: Path, expected_signer: str) -> dict[str, str]:
    result = run([apksigner, "verify", "--verbose", "--print-certs", apk], timeout=180)
    signers = re.findall(
        r"Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})", result.stdout
    )
    if len(signers) != 1:
        raise SetupError("apksigner did not report exactly one recognizable signer certificate")
    signer = signers[0].lower()
    if signer != expected_signer:
        raise SetupError(f"Obsidian APK signer mismatch: expected {expected_signer}, got {signer}")
    if "Verified using v2 scheme (APK Signature Scheme v2): true" not in result.stdout:
        raise SetupError("Obsidian APK did not verify with APK Signature Scheme v2")
    return {"signerSha256": signer, "schemeV2": "true"}


def obtain_apk(config: SetupConfig, apksigner: Path) -> tuple[Path, dict[str, str]]:
    if config.apk:
        if not config.apk.is_file():
            raise SetupError(f"OBSIDIAN_APK does not exist: {config.apk}")
        apk = config.apk
        version = "local"
        expected_digest = _hash_file(apk, "sha256")
    else:
        asset = latest_obsidian_asset()
        apk = config.state_dir / "downloads" / asset.name
        version = asset.version
        expected_digest = asset.sha256
        if not apk.exists() or _hash_file(apk, "sha256") != expected_digest:
            _download(asset.url, apk)
    actual_digest = _hash_file(apk, "sha256")
    if actual_digest != expected_digest:
        raise SetupError(f"Obsidian APK digest mismatch: expected {expected_digest}, got {actual_digest}")
    signature = verify_apk_signature(apksigner, apk, config.signer_sha256)
    return apk, {
        "version": version,
        "sha256": actual_digest,
        **signature,
    }


def configure_webview_flags(
    config: SetupConfig, adb: Path, flags: Sequence[str] = SOFTWARE_WEBVIEW_FLAGS,
) -> list[str]:
    """Install verified userdebug-only WebView flags.

    The software emulator disables Chromium's hang monitor because TCG can look
    dead while making progress. The native container enables verbose logging
    only. CDP and process-stability checks remain authoritative in both cases.
    """
    build_type = _adb_text(
        adb, config.serial, "shell", "getprop", "ro.build.type", timeout=30
    )
    if build_type not in {"userdebug", "eng"}:
        raise SetupError(
            "WebView command-line configuration requires an Android userdebug or eng image; "
            f"refusing to modify a {build_type or 'unknown'} build"
        )

    contents = f"_ {' '.join(flags)}\n"
    with tempfile.TemporaryDirectory(prefix="omd-webview-") as temporary:
        source = Path(temporary) / "webview-command-line"
        source.write_text(contents)
        _adb(adb, config.serial, "push", str(source), f"{WEBVIEW_COMMAND_LINE}.new", timeout=60)
    _adb(
        adb, config.serial, "shell", "mv", f"{WEBVIEW_COMMAND_LINE}.new",
        WEBVIEW_COMMAND_LINE, timeout=30,
    )
    _adb(
        adb, config.serial, "shell", "chown", "root:root", WEBVIEW_COMMAND_LINE,
        timeout=30,
    )
    _adb(
        adb, config.serial, "shell", "chmod", "0644", WEBVIEW_COMMAND_LINE,
        timeout=30,
    )
    installed = _adb_text(
        adb, config.serial, "shell", "cat", WEBVIEW_COMMAND_LINE, timeout=30
    )
    if installed != contents.strip():
        raise SetupError(
            "Failed to verify the isolated AVD's WebView command-line file: "
            f"expected {contents.strip()!r}, got {installed!r}"
        )
    return list(flags)


def launch_emulator(config: SetupConfig, command: Sequence[str]) -> tuple[int, Path]:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.state_dir / f"{config.avd}-emulator.log"
    log = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            list(command),
            env=config.env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    (config.state_dir / f"{config.avd}.pid").write_text(f"{process.pid}\n")
    time.sleep(2)
    if process.poll() is not None:
        tail = log_path.read_text(errors="replace").splitlines()[-40:]
        tail_text = "\n".join(tail)
        raise SetupError(f"Android emulator exited during startup:\n{tail_text}")
    return process.pid, log_path


def terminate_launched_emulator(
    pid: int, adb: Path | None = None, serial: str | None = None,
) -> None:
    """Shut down the launched emulator cleanly, then use scoped fallbacks."""
    if adb is not None and serial is not None:
        try:
            _adb(adb, serial, "shell", "sync", check=False, timeout=120)
            _adb(adb, serial, "shell", "reboot", "-p", check=False, timeout=30)
        except Exception:
            pass
        graceful_deadline = time.monotonic() + 60
        while time.monotonic() < graceful_deadline:
            try:
                waited, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if waited == pid:
                return
            time.sleep(0.25)
        try:
            _adb(adb, serial, "emu", "kill", check=False, timeout=30)
        except Exception:
            pass
        console_deadline = time.monotonic() + 30
        while time.monotonic() < console_deadline:
            try:
                waited, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if waited == pid:
                return
            time.sleep(0.25)

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        time.sleep(0.25)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _running_avd(adb: Path, serial: str) -> str | None:
    state = _adb_text(adb, serial, "get-state", check=False, timeout=10)
    if state not in {"device", "offline"}:
        return None
    name = _adb_text(adb, serial, "emu", "avd", "name", check=False, timeout=10)
    return name.splitlines()[0].strip() if name else None


def install_and_launch(
    config: SetupConfig, adb: Path, apk: Path,
) -> tuple[int, str]:
    _adb(adb, config.serial, "install", "-r", str(apk), timeout=900)
    _adb(adb, config.serial, "logcat", "-c", check=False, timeout=30)
    _adb(adb, config.serial, "shell", "am", "start", "-n", f"{config.bundle}/.MainActivity", timeout=120)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        pid_text = _adb_text(adb, config.serial, "shell", "pidof", config.bundle, check=False, timeout=10)
        if pid_text:
            pid = int(pid_text.split()[0])
            sockets = _adb_text(
                adb, config.serial, "shell", "cat", "/proc/net/unix", check=False, timeout=30
            )
            socket = f"webview_devtools_remote_{pid}"
            if socket in sockets:
                return pid, socket
        time.sleep(5)
    raise SetupError("Obsidian launched but did not expose a WebView DevTools socket within 300s")


def verify_android_sandbox(
    config: SetupConfig, adb: Path, app_pid: int,
) -> dict[str, Any]:
    """Prove the installed app runs under its package UID with multiprocess WebView."""
    package = _adb_text(
        adb, config.serial, "shell", "dumpsys", "package", config.bundle, timeout=120
    )
    package_uid_match = re.search(r"\b(?:userId|appId)=(\d+)\b", package)
    if not package_uid_match:
        raise SetupError(f"Could not determine Android package UID for {config.bundle}")
    package_uid = int(package_uid_match.group(1))
    status = _adb_text(
        adb, config.serial, "shell", "cat", f"/proc/{app_pid}/status", timeout=30
    )
    process_uid_match = re.search(r"^Uid:\s+(\d+)", status, re.MULTILINE)
    if not process_uid_match:
        raise SetupError(f"Could not determine Linux UID for Obsidian process {app_pid}")
    process_uid = int(process_uid_match.group(1))
    if package_uid < 10_000 or process_uid != package_uid:
        raise SetupError(
            "Obsidian did not run under its isolated Android application UID: "
            f"package={package_uid}, process={process_uid}"
        )
    webview = _adb_text(
        adb, config.serial, "shell", "dumpsys", "webviewupdate", timeout=120
    )
    if "Multiprocess enabled: true" not in webview:
        raise SetupError("Android WebView multiprocess isolation is not enabled")
    webview_package = re.search(
        r"Current WebView package \(name, version\): \(([^,]+), ([^)]+)\)", webview
    )
    if not webview_package:
        raise SetupError("Could not identify the active Android WebView package")
    return {
        "packageUid": package_uid,
        "processUid": process_uid,
        "webviewMultiprocess": True,
        "webviewPackage": webview_package.group(1),
        "webviewVersion": webview_package.group(2),
    }


def verify_cdp_runtime(
    config: SetupConfig, adb: Path, app_pid: int, socket: str,
) -> dict[str, Any]:
    """Require two evaluations against one stable, crash-free CDP target.

    Obsidian replaces its initial WebView during first-run startup. On a TCG
    host that transition can take minutes and close an otherwise healthy CDP
    connection. Retry target-lifecycle disconnects, but only while the same app
    process remains alive and logcat has no renderer/app crash marker.
    """
    from .android import discover_page_ws, ev

    # Give the WebView a quiet startup window before DevTools attaches. This is
    # cheap under acceleration and prevents CDP from competing with first-load
    # renderer initialization under TCG.
    time.sleep(30)
    forwarded = _adb_text(
        adb, config.serial, "forward", "tcp:0", f"localabstract:{socket}", timeout=30
    )
    try:
        port = int(forwarded.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SetupError(f"ADB did not allocate a CDP forwarding port: {forwarded!r}") from exc

    results: list[Any] = []
    target_url = ""
    transitions = 0
    deadline = time.monotonic() + 600
    try:
        while time.monotonic() < deadline:
            try:
                target, target_url = discover_page_ws(port)
                first = asyncio.run(ev(port, "1+1", timeout=300))
                target_after_first, _url = discover_page_ws(port)
            except (Exception, SystemExit):
                _assert_cdp_health(config, adb, app_pid)
                transitions += 1
                time.sleep(10)
                continue
            if first != 2:
                raise SetupError(f"Obsidian CDP returned an unexpected smoke result: {first!r}")
            if target_after_first != target:
                _assert_cdp_health(config, adb, app_pid)
                transitions += 1
                time.sleep(10)
                continue

            time.sleep(30)
            try:
                target_before_second, _url = discover_page_ws(port)
                if target_before_second != target:
                    _assert_cdp_health(config, adb, app_pid)
                    transitions += 1
                    continue
                second = asyncio.run(ev(port, "1+1", timeout=300))
                target_after_second, _url = discover_page_ws(port)
            except (Exception, SystemExit):
                _assert_cdp_health(config, adb, app_pid)
                transitions += 1
                time.sleep(10)
                continue
            if second != 2:
                raise SetupError(f"Obsidian CDP returned an unexpected smoke result: {second!r}")
            if target_after_second != target:
                _assert_cdp_health(config, adb, app_pid)
                transitions += 1
                time.sleep(10)
                continue
            results = [first, second]
            break
        else:
            raise SetupError(
                "Obsidian did not expose one stable CDP page for 30 seconds within 600s "
                f"({transitions} target transitions)"
            )
    finally:
        _adb(adb, config.serial, "forward", "--remove", f"tcp:{port}", check=False, timeout=30)

    _assert_cdp_health(config, adb, app_pid)
    return {
        "evaluations": results,
        "warmupSeconds": 30,
        "stabilitySeconds": 30,
        "targetUrl": target_url,
        "startupTargetTransitions": transitions,
    }


def _assert_cdp_health(config: SetupConfig, adb: Path, app_pid: int) -> None:
    """Reject CDP retries if the app died or logcat reports a real crash."""
    app_pids = _adb_text(
        adb, config.serial, "shell", "pidof", config.bundle, check=False, timeout=30
    ).split()
    if str(app_pid) not in app_pids:
        raise SetupError(
            f"Obsidian process {app_pid} did not survive the CDP stability check: {app_pids}"
        )
    logs = _adb_text(
        adb, config.serial, "logcat", "-d", "-v", "brief", check=False, timeout=120
    )
    marker = next((value for value in APP_CRASH_MARKERS if value in logs), None)
    if marker:
        raise SetupError(f"Obsidian/WebView crash detected during CDP stability check: {marker}")


def capture_failure_diagnostics(config: SetupConfig, adb: Path) -> Path | None:
    """Persist isolated-runtime logcat before failure cleanup removes access."""
    try:
        result = _adb(
            adb, config.serial, "logcat", "-d", "-v", "threadtime", check=False, timeout=120
        )
        if not result.stdout.strip():
            return None
        path = config.state_dir / f"{config.avd}-failure-logcat.txt"
        path.write_text(result.stdout)
        path.chmod(0o600)
        return path
    except Exception:
        return None


def setup(config: SetupConfig) -> dict[str, Any]:
    tools = ensure_sdk(config)
    pid: int | None = None
    docker: list[str] | None = None
    container_launched = False
    container_report: dict[str, Any] | None = None
    try:
        if config.backend == "container":
            acceleration = "native-container"
            log_path: Path | None = None
            docker, container_launched, container_report = ensure_redroid(config)
            connect_container_adb(config, tools["adb"])
            root = _adb_text(tools["adb"], config.serial, "root", check=False, timeout=120)
            if "cannot run as root" in root.lower():
                raise SetupError("The pinned ReDroid image did not allow its expected adb root")
            wait_for_adb(tools["adb"], config.serial, config.adb_timeout)
        else:
            ensure_avd(config, tools["avdmanager"], tools["emulator"])
            acceleration = choose_acceleration(
                config.acceleration, tools["emulator"], config.env
            )
            existing = _running_avd(tools["adb"], config.serial)
            if existing and existing != config.avd:
                raise SetupError(
                    f"{config.serial} already belongs to AVD {existing!r}; "
                    "stop it or choose another --console-port"
                )
            log_path = config.state_dir / f"{config.avd}-emulator.log"
            if not existing:
                command = emulator_command(config, tools["emulator"], acceleration)
                pid, log_path = launch_emulator(config, command)

            if acceleration == "off":
                bootstrap_software_emulator(config, tools["adb"])
            else:
                wait_for_adb(tools["adb"], config.serial, config.adb_timeout)
        stable = wait_for_stable_boot(config, tools["adb"], acceleration)

        webview_flags: list[str] = []
        if config.backend == "container":
            webview_flags = configure_webview_flags(
                config, tools["adb"], ("--webview-verbose-logging",)
            )
        elif acceleration == "off":
            webview_flags = configure_webview_flags(config, tools["adb"])

        apk, apk_report = obtain_apk(config, tools["apksigner"])
        app_pid, socket = install_and_launch(config, tools["adb"], apk)
        sandbox_report = verify_android_sandbox(config, tools["adb"], app_pid)
        cdp_report = verify_cdp_runtime(config, tools["adb"], app_pid, socket)
        final_state = boot_state(config, tools["adb"], acceleration)
        if final_state["ready"] != "true":
            raise SetupError(f"Android became unhealthy after installing Obsidian: {final_state}")
        if final_state["systemServer"] != stable["systemServer"]:
            raise SetupError(
                "Android system_server restarted while installing Obsidian: "
                f"before={stable['systemServer']}, after={final_state['systemServer']}"
            )
        logs = _adb_text(
            tools["adb"], config.serial, "logcat", "-d", "-v", "brief",
            check=False, timeout=120,
        )
        marker = next((value for value in WATCHDOG_MARKERS if value in logs), None)
        if marker:
            raise SetupError(f"Android watchdog failure detected after installing Obsidian: {marker}")

        return {
            "ok": True,
            "backend": config.backend,
            "platform": platform.system(),
            "sdkRoot": str(config.sdk_root),
            "androidHome": str(config.android_home),
            "avd": config.avd,
            "serial": config.serial,
            "emulatorPid": pid,
            "emulatorLog": str(log_path) if log_path else None,
            "container": container_report,
            "api": config.api,
            "abi": config.abi,
            "acceleration": acceleration,
            "emulatorCores": (
                min(os.cpu_count() or 2, 4) if config.backend == "emulator" else None
            ),
            "softwareCpu": (
                SOFTWARE_CPU
                if config.backend == "emulator" and acceleration == "off"
                else None
            ),
            "webviewFlags": webview_flags,
            "boot": final_state,
            "security": {
                "selinux": final_state["selinux"],
                "adbBinding": (
                    container_report["adbBinding"] if container_report else config.serial
                ),
                "testOnlyPrivilegedContainer": config.backend == "container",
                "androidSandbox": sandbox_report,
            },
            "apk": {"path": str(apk), **apk_report},
            "obsidian": {
                "pid": app_pid,
                "devtoolsSocket": socket,
                "cdp": cdp_report,
            },
        }
    except BaseException:
        diagnostics = capture_failure_diagnostics(config, tools["adb"])
        if diagnostics:
            print(f"failure logcat: {diagnostics}", file=sys.stderr, flush=True)
        if pid is not None:
            terminate_launched_emulator(pid, tools["adb"], config.serial)
        if container_launched and docker is not None:
            run([*docker, "rm", "-f", config.container_name], check=False, timeout=60)
        raise


def setup_from_args(args: Any) -> int:
    try:
        report = setup(config_from_args(args))
    except KeyboardInterrupt:
        print("Android setup interrupted; launched runtime cleanup completed", file=sys.stderr)
        return 130
    except SetupError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0
