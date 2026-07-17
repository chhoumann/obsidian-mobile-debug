"""Android runtime provisioning helpers, isolated from network and devices."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from obsidian_mobile_debug import android_setup


def config(tmp_path: Path, **overrides) -> android_setup.SetupConfig:
    values = {
        "backend": "emulator",
        "api": 34,
        "abi": "x86_64",
        "avd": "obsidian-debug",
        "device": "pixel_6",
        "bundle": "md.obsidian",
        "gpu": "auto",
        "sdk_root": tmp_path / "sdk",
        "android_home": tmp_path / "android-home",
        "state_dir": tmp_path / "state",
        "apk": None,
        "acceleration": "auto",
        "adb_timeout": 1200,
        "boot_timeout": 2400,
        "console_port": 5554,
        "adb_port": 5555,
        "reset": False,
        "timeout_multiplier": 50,
        "signer_sha256": android_setup.OBSIDIAN_SIGNER_SHA256,
        "acknowledge_privileged_container": False,
    }
    values.update(overrides)
    return android_setup.SetupConfig(**values)


def test_default_sdk_root_is_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(android_setup.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    assert android_setup.default_sdk_root({}) == tmp_path / ".cache/obsidian-mobile-debug/android-sdk"


@pytest.mark.parametrize(
    "url",
    [
        "https://dl.google.com/android/repository/tools.zip",
        "https://github.com/obsidianmd/obsidian-releases/releases/download/v1/app.apk",
        "https://release-assets.githubusercontent.com/file",
    ],
)
def test_validate_download_url_accepts_pinned_hosts(url):
    android_setup._validate_download_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/file",
        "file:///tmp/app.apk",
        "https://github.com.evil.example/app.apk",
        "https://evil.example/app.apk",
    ],
)
def test_validate_download_url_rejects_untrusted_urls(url):
    with pytest.raises(android_setup.SetupError, match="untrusted"):
        android_setup._validate_download_url(url)


def test_config_from_args_normalizes_signer(tmp_path):
    args = argparse.Namespace(
        api=34, abi="auto", avd="test", device="pixel_6", bundle="md.obsidian", gpu="auto",
        sdk_root=str(tmp_path / "sdk"), android_home=None, state_dir=str(tmp_path / "state"),
        apk=None, acceleration="off", adb_timeout=1, boot_timeout=2, console_port=5554,
        reset=False, timeout_multiplier=50,
        signer_sha256="BD:3B:D5:2F:44:27:B5:AB:7F:80:59:D0:71:A3:5E:3D:E9:43:C6:46:54:6D:32:31:3D:A5:F1:CA:C2:54:B7:E4",
    )
    result = android_setup.config_from_args(args)
    assert result.signer_sha256 == android_setup.OBSIDIAN_SIGNER_SHA256
    assert result.android_home == tmp_path / "state/android-user"


def test_container_config_requires_explicit_security_acknowledgement(monkeypatch, tmp_path):
    monkeypatch.setattr(android_setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(android_setup.platform, "machine", lambda: "x86_64")
    args = argparse.Namespace(
        backend="container", api=34, abi="auto", avd="test", device="pixel_6",
        bundle="md.obsidian", gpu="auto", sdk_root=str(tmp_path / "sdk"),
        android_home=None, state_dir=str(tmp_path / "state"), apk=None,
        acceleration="off", adb_timeout=1, boot_timeout=2, console_port=5554,
        adb_port=5555, reset=False, timeout_multiplier=50,
        signer_sha256=android_setup.OBSIDIAN_SIGNER_SHA256,
        acknowledge_privileged_container=False,
    )
    with pytest.raises(android_setup.SetupError, match="acknowledge-privileged-container"):
        android_setup.config_from_args(args)


def test_container_config_uses_loopback_serial(monkeypatch, tmp_path):
    monkeypatch.setattr(android_setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(android_setup.platform, "machine", lambda: "x86_64")
    args = argparse.Namespace(
        backend="container", api=34, abi="auto", avd="test", device="pixel_6",
        bundle="md.obsidian", gpu="auto", sdk_root=str(tmp_path / "sdk"),
        android_home=None, state_dir=str(tmp_path / "state"), apk=None,
        acceleration="off", adb_timeout=1, boot_timeout=2, console_port=5554,
        adb_port=5565, reset=False, timeout_multiplier=50,
        signer_sha256=android_setup.OBSIDIAN_SIGNER_SHA256,
        acknowledge_privileged_container=True,
    )
    result = android_setup.config_from_args(args)
    assert result.backend == "container"
    assert result.serial == "127.0.0.1:5565"
    assert result.container_name == "omd-test-redroid"


@pytest.mark.parametrize("uid_field", ["userId", "appId"])
def test_verify_android_sandbox_accepts_package_uid_forms(monkeypatch, tmp_path, uid_field):
    responses = iter([
        f"Package [md.obsidian]\n  {uid_field}=10123",
        "Name:\tmd.obsidian\nUid:\t10123\t10123\t10123\t10123",
        "Multiprocess enabled: true\n"
        "Current WebView package (name, version): (com.android.webview, 125.0.1)",
    ])
    monkeypatch.setattr(android_setup, "_adb_text", lambda *_args, **_kwargs: next(responses))
    result = android_setup.verify_android_sandbox(config(tmp_path), Path("/adb"), 42)
    assert result["packageUid"] == 10123
    assert result["processUid"] == 10123
    assert result["webviewMultiprocess"] is True


def test_ensure_redroid_pins_image_and_loopback_binding(monkeypatch, tmp_path):
    binder = tmp_path / "binder"
    binder.touch()
    monkeypatch.setattr(android_setup, "REDROID_BINDER_DEVICE", binder)
    monkeypatch.setattr(android_setup, "docker_prefix", lambda: ["docker"])
    commands = []

    def fake_run(command, **_kwargs):
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(rendered, 0, f"{android_setup.REDROID_IMAGE_DIGEST} amd64\n")
        if rendered[1] == "inspect" and "HostConfig.Privileged" not in rendered[-1]:
            return subprocess.CompletedProcess(rendered, 1, "")
        if rendered[1] == "inspect":
            data = (tmp_path / "state/redroid/obsidian-debug/data").resolve()
            identity = f"true\n{android_setup.REDROID_IMAGE}\ntrue\n{data}\n"
            return subprocess.CompletedProcess(rendered, 0, identity)
        if rendered[1] == "port":
            return subprocess.CompletedProcess(rendered, 0, "127.0.0.1:5555\n")
        return subprocess.CompletedProcess(rendered, 0, "ok\n")

    monkeypatch.setattr(android_setup, "run", fake_run)
    cfg = config(
        tmp_path, backend="container", acknowledge_privileged_container=True
    )
    _docker, launched, report = android_setup.ensure_redroid(cfg)
    docker_run = next(command for command in commands if command[1:3] == ["run", "-d"])
    assert launched is True
    assert "--privileged" in docker_run
    assert "127.0.0.1:5555:5555" in docker_run
    assert android_setup.REDROID_IMAGE in docker_run
    assert report["imageDigest"] == android_setup.REDROID_IMAGE_DIGEST
    assert report["adbBinding"] == "127.0.0.1:5555"


def test_ensure_redroid_rejects_public_adb_binding(monkeypatch, tmp_path):
    binder = tmp_path / "binder"
    binder.touch()
    monkeypatch.setattr(android_setup, "REDROID_BINDER_DEVICE", binder)
    monkeypatch.setattr(android_setup, "docker_prefix", lambda: ["docker"])
    removed = []

    def fake_run(command, **_kwargs):
        rendered = [str(part) for part in command]
        if rendered[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(rendered, 0, f"{android_setup.REDROID_IMAGE_DIGEST} amd64\n")
        if rendered[1] == "inspect" and "HostConfig.Privileged" not in rendered[-1]:
            return subprocess.CompletedProcess(rendered, 1, "")
        if rendered[1] == "inspect":
            data = (tmp_path / "state/redroid/obsidian-debug/data").resolve()
            identity = f"true\n{android_setup.REDROID_IMAGE}\ntrue\n{data}\n"
            return subprocess.CompletedProcess(rendered, 0, identity)
        if rendered[1] == "port":
            return subprocess.CompletedProcess(rendered, 0, "0.0.0.0:5555\n")
        if rendered[1:3] == ["rm", "-f"]:
            removed.append(rendered[-1])
        return subprocess.CompletedProcess(rendered, 0, "ok\n")

    monkeypatch.setattr(android_setup, "run", fake_run)
    cfg = config(
        tmp_path, backend="container", acknowledge_privileged_container=True
    )
    with pytest.raises(android_setup.SetupError, match="Refusing non-loopback"):
        android_setup.ensure_redroid(cfg)
    assert removed == [cfg.container_name]


@pytest.mark.parametrize("port", [5553, 5555, 5684])
def test_config_from_args_rejects_invalid_console_port(tmp_path, port):
    args = argparse.Namespace(
        api=34, abi="auto", avd="test", device="pixel_6", bundle="md.obsidian", gpu="auto",
        sdk_root=str(tmp_path / "sdk"), android_home=None, state_dir=str(tmp_path / "state"),
        apk=None, acceleration="off", adb_timeout=1, boot_timeout=2, console_port=port,
        reset=False, timeout_multiplier=50,
        signer_sha256=android_setup.OBSIDIAN_SIGNER_SHA256,
    )
    with pytest.raises(android_setup.SetupError, match="console-port"):
        android_setup.config_from_args(args)


def test_software_emulator_command_is_headless_and_unsnapshotted(monkeypatch, tmp_path):
    monkeypatch.setattr(android_setup.os, "cpu_count", lambda: 4)
    command = android_setup.emulator_command(config(tmp_path), Path("/sdk/emulator"), "off")
    assert command[:3] == ["/sdk/emulator", "-avd", "obsidian-debug"]
    assert command[command.index("-accel") + 1] == "off"
    assert "-no-window" in command
    assert command[command.index("-gpu") + 1] == "software"
    assert "-no-snapshot" in command
    assert command[command.index("-selinux") + 1] == "permissive"
    assert command[command.index("-cores") + 1] == "4"
    assert command[-3:] == ["-qemu", "-cpu", "max"]


def test_accelerated_emulator_does_not_boot_permissive(monkeypatch, tmp_path):
    monkeypatch.setattr(android_setup.os, "cpu_count", lambda: 4)
    command = android_setup.emulator_command(config(tmp_path), Path("/sdk/emulator"), "on")
    assert "-selinux" not in command
    assert command[command.index("-cores") + 1] == "4"
    assert command[command.index("-gpu") + 1] == "auto"


def test_setup_from_args_handles_interrupt_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(android_setup, "config_from_args", lambda _args: object())
    monkeypatch.setattr(
        android_setup,
        "setup",
        lambda _config: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    assert android_setup.setup_from_args(object()) == 130
    assert "cleanup completed" in capsys.readouterr().err


def test_choose_acceleration_uses_emulator_result(monkeypatch):
    monkeypatch.setattr(
        android_setup, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], returncode=1, stdout="no KVM"),
    )
    assert android_setup.choose_acceleration("auto", Path("emulator"), {}) == "off"
    assert android_setup.choose_acceleration("on", Path("emulator"), {}) == "on"


def test_select_release_asset_requires_github_digest():
    payload = {
        "tag_name": "v1.2.3",
        "assets": [{
            "name": "Obsidian-1.2.3.apk",
            "browser_download_url": "https://example.invalid/obsidian.apk",
            "digest": "sha256:" + "a" * 64,
        }],
    }
    asset = android_setup.select_release_asset(payload)
    assert asset.version == "v1.2.3"
    assert asset.sha256 == "a" * 64


@pytest.mark.parametrize("digest", [None, "sha1:" + "a" * 40, "sha256:short"])
def test_select_release_asset_rejects_missing_or_weak_digest(digest):
    payload = {
        "assets": [{
            "name": "Obsidian.apk",
            "browser_download_url": "https://example.invalid/obsidian.apk",
            "digest": digest,
        }],
    }
    with pytest.raises(android_setup.SetupError, match="SHA-256"):
        android_setup.select_release_asset(payload)


def test_verify_apk_signature_requires_one_expected_v2_signer(monkeypatch, tmp_path):
    output = (
        "Verified using v2 scheme (APK Signature Scheme v2): true\n"
        f"Signer #1 certificate SHA-256 digest: {android_setup.OBSIDIAN_SIGNER_SHA256}\n"
    )
    monkeypatch.setattr(
        android_setup, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], returncode=0, stdout=output),
    )
    report = android_setup.verify_apk_signature(
        Path("apksigner"), tmp_path / "Obsidian.apk", android_setup.OBSIDIAN_SIGNER_SHA256
    )
    assert report == {
        "signerSha256": android_setup.OBSIDIAN_SIGNER_SHA256,
        "schemeV2": "true",
    }


def test_verify_apk_signature_rejects_multiple_signers(monkeypatch, tmp_path):
    output = (
        "Verified using v2 scheme (APK Signature Scheme v2): true\n"
        f"Signer #1 certificate SHA-256 digest: {android_setup.OBSIDIAN_SIGNER_SHA256}\n"
        f"Signer #2 certificate SHA-256 digest: {'a' * 64}\n"
    )
    monkeypatch.setattr(
        android_setup, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], returncode=0, stdout=output),
    )
    with pytest.raises(android_setup.SetupError, match="exactly one"):
        android_setup.verify_apk_signature(
            Path("apksigner"), tmp_path / "Obsidian.apk", android_setup.OBSIDIAN_SIGNER_SHA256
        )


def test_configure_webview_flags_uses_verified_userdebug_file(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []

    def fake_adb(_adb, _serial, *args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], returncode=0, stdout="")

    def fake_adb_text(_adb, _serial, *args, **_kwargs):
        calls.append(args)
        if args == ("shell", "getprop", "ro.build.type"):
            return "userdebug"
        if args == ("shell", "cat", android_setup.WEBVIEW_COMMAND_LINE):
            return "_ --disable-hang-monitor --webview-verbose-logging"
        raise AssertionError(args)

    monkeypatch.setattr(android_setup, "_adb", fake_adb)
    monkeypatch.setattr(android_setup, "_adb_text", fake_adb_text)

    flags = android_setup.configure_webview_flags(config(tmp_path), Path("adb"))

    assert flags == ["--disable-hang-monitor", "--webview-verbose-logging"]
    push = next(call for call in calls if call[0] == "push")
    assert push[-1] == f"{android_setup.WEBVIEW_COMMAND_LINE}.new"
    assert (
        "shell", "chmod", "0644", android_setup.WEBVIEW_COMMAND_LINE,
    ) in calls


def test_configure_webview_flags_rejects_user_build(monkeypatch, tmp_path):
    monkeypatch.setattr(
        android_setup, "_adb_text",
        lambda *_args, **_kwargs: "user",
    )
    with pytest.raises(android_setup.SetupError, match="refusing to modify a user build"):
        android_setup.configure_webview_flags(config(tmp_path), Path("adb"))


def test_verify_cdp_runtime_retries_startup_target_replacement(monkeypatch, tmp_path):
    from obsidian_mobile_debug import android

    targets = iter([
        ("ws://target-1", "file:///first"),
        ("ws://target-2", "file:///second"),
        ("ws://target-2", "file:///second"),
        ("ws://target-2", "file:///second"),
        ("ws://target-2", "file:///second"),
        ("ws://target-2", "file:///second"),
    ])

    async def fake_ev(_port, _expr, **_kwargs):
        return 2

    def fake_adb_text(_adb, _serial, *args, **_kwargs):
        if args[:2] == ("forward", "tcp:0"):
            return "4567"
        if args == ("shell", "pidof", "md.obsidian"):
            return "123"
        if args[:2] == ("logcat", "-d"):
            return "healthy"
        raise AssertionError(args)

    monkeypatch.setattr(android, "discover_page_ws", lambda _port: next(targets))
    monkeypatch.setattr(android, "ev", fake_ev)
    monkeypatch.setattr(android_setup, "_adb_text", fake_adb_text)
    monkeypatch.setattr(
        android_setup, "_adb",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode=0, stdout=""),
    )
    monkeypatch.setattr(android_setup.time, "sleep", lambda _seconds: None)

    report = android_setup.verify_cdp_runtime(
        config(tmp_path), Path("adb"), 123, "webview_devtools_remote_123"
    )

    assert report["evaluations"] == [2, 2]
    assert report["startupTargetTransitions"] == 1
    assert report["targetUrl"] == "file:///second"


def test_cdp_health_rejects_renderer_crash_marker(monkeypatch, tmp_path):
    def fake_adb_text(_adb, _serial, *args, **_kwargs):
        if args == ("shell", "pidof", "md.obsidian"):
            return "123"
        if args[:2] == ("logcat", "-d"):
            return "Renderer process (42) crash detected"
        raise AssertionError(args)

    monkeypatch.setattr(android_setup, "_adb_text", fake_adb_text)
    with pytest.raises(android_setup.SetupError, match="Renderer process"):
        android_setup._assert_cdp_health(config(tmp_path), Path("adb"), 123)


def test_emulator_teardown_requests_guest_sync_and_poweroff(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        android_setup, "_adb",
        lambda _adb, _serial, *args, **_kwargs: calls.append(args),
    )
    monkeypatch.setattr(android_setup.os, "waitpid", lambda _pid, _flags: (42, 0))
    monkeypatch.setattr(
        android_setup.os, "killpg",
        lambda *_args: pytest.fail("signal fallback should not run after a graceful exit"),
    )

    android_setup.terminate_launched_emulator(42, Path("adb"), "emulator-5554")

    assert calls == [("shell", "sync"), ("shell", "reboot", "-p")]


def test_boot_state_requires_security_and_health_invariants(monkeypatch, tmp_path):
    responses = {
        ("get-state",): "device",
        ("shell", "getprop", "sys.boot_completed"): "1",
        ("shell", "pidof", "system_server"): "123",
        ("shell", "service", "check", "package"): "Service package: found",
        ("shell", "getenforce"): "Enforcing",
        ("shell", "getprop", "ro.hw_timeout_multiplier"): "50",
    }
    monkeypatch.setattr(
        android_setup, "_adb_text",
        lambda _adb, _serial, *args, **kwargs: responses[args],
    )
    assert android_setup.boot_state(config(tmp_path), Path("adb"), "off")["ready"] == "true"
    responses[("shell", "getenforce")] = "Permissive"
    assert android_setup.boot_state(config(tmp_path), Path("adb"), "off")["ready"] == "false"


def test_software_bootstrap_does_not_interrupt_apex_before_system_server(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(android_setup, "wait_for_adb", lambda *_args: None)

    def fake_adb(_adb, _serial, *args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], returncode=0, stdout="")

    def fake_adb_text(_adb, _serial, *args, **_kwargs):
        calls.append(args)
        responses = {
            ("root",): "restarting adbd as root",
            ("shell", "getprop", "ro.hw_timeout_multiplier"): "50",
            ("shell", "getenforce"): "Enforcing",
            ("shell", "pidof", "system_server"): "",
        }
        return responses[args]

    monkeypatch.setattr(android_setup, "_adb", fake_adb)
    monkeypatch.setattr(android_setup, "_adb_text", fake_adb_text)
    android_setup.bootstrap_software_emulator(config(tmp_path), Path("adb"))

    assert ("shell", "stop") not in calls
    assert ("shell", "start") not in calls
    assert calls.index(("shell", "setprop", "ro.hw_timeout_multiplier", "50")) \
        < calls.index(("shell", "setenforce", "1"))


def test_software_bootstrap_restarts_services_if_system_server_already_exists(
    monkeypatch, tmp_path,
):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(android_setup, "wait_for_adb", lambda *_args: None)

    def fake_adb(_adb, _serial, *args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], returncode=0, stdout="")

    def fake_adb_text(_adb, _serial, *args, **_kwargs):
        calls.append(args)
        responses = {
            ("root",): "restarting adbd as root",
            ("shell", "getprop", "ro.hw_timeout_multiplier"): "50",
            ("shell", "getenforce"): "Enforcing",
            ("shell", "pidof", "system_server"): "123",
        }
        return responses[args]

    monkeypatch.setattr(android_setup, "_adb", fake_adb)
    monkeypatch.setattr(android_setup, "_adb_text", fake_adb_text)
    android_setup.bootstrap_software_emulator(config(tmp_path), Path("adb"))

    assert calls.index(("shell", "stop")) < calls.index(("shell", "start"))
