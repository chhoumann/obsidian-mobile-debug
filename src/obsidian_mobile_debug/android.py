"""Android transport: drive Obsidian's Chromium WebView over Chrome DevTools.

Android's WebView speaks standard CDP (unlike iOS's WIP), so once the app's
``webview_devtools_remote_<pid>`` unix socket is forwarded to a localhost TCP
port, ``Runtime.evaluate`` does everything. This module owns:

- socket auto-discovery (``adb shell cat /proc/net/unix``, falling back to
  ``pidof`` to construct the name),
- automatic ``adb forward`` setup and teardown (configurable local port),
- ONE shared CDP ``ev()`` (awaitPromise) used by every subcommand.

adb is invoked as a subprocess; websockets is imported lazily. Neither is
touched during argument parsing, so ``--help`` works without a device. Broad
``except`` blocks re-raise ``SystemExit`` with a concrete next step on purpose:
adb/CDP errors are opaque and the hint is the value.

Android has no backup/restore path (adb push is not snapshot-verified), so
``deploy`` targets a disposable scratch vault and requires --confirm-real-vault.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_PACKAGE = "md.obsidian"
DEFAULT_CDP_PORT = 9333
SAFE_VAULT_TOKENS = ("test", "scratch", "debug", "sandbox")


# ---------- pure helpers (no device, safe to import/unit-test) ----------
def js(value: str) -> str:
    return json.dumps(value)


def adb_bin() -> str:
    return os.environ.get("ADB", "adb")


def forward_command(port: int, socket: str) -> list[str]:
    return ["forward", f"tcp:{port}", f"localabstract:{socket}"]


def forward_remove_command(port: int) -> list[str]:
    return ["forward", "--remove", f"tcp:{port}"]


def normalize_android_path(path: str) -> str:
    normalized = posixpath.normpath(path.strip())
    if not normalized.startswith("/"):
        raise SystemExit(f"Android vault path must be absolute: {path!r}")
    return normalized


def looks_like_test_vault(name: str | None, expected: str | None = None) -> bool:
    if not name:
        return False
    if expected and name == expected:
        return True
    lowered = name.lower()
    return any(token in lowered for token in SAFE_VAULT_TOKENS)


def guard_real_vault(vault_name: str, args: argparse.Namespace, operation: str) -> None:
    if getattr(args, "confirm_real_vault", False):
        return
    if looks_like_test_vault(vault_name, getattr(args, "test_vault", None)):
        return
    raise SystemExit(
        f"Refusing to {operation} against vault {vault_name!r}: it does not look like a test "
        f"vault (name contains none of {SAFE_VAULT_TOKENS}). Android deploy has no backup/restore "
        f"path.\nRe-run with --confirm-real-vault to proceed, or --test-vault {vault_name!r} to "
        f"whitelist this name."
    )


# ---------- adb ----------
def run_adb(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [adb_bin(), *args], check=check, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            f"adb not found ({adb_bin()!r}). Install Android platform-tools and/or set the ADB "
            "environment variable to its path."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"adb {' '.join(args)} failed: {exc.stderr.strip() or exc}") from exc


def adb_out(args: list[str], *, check: bool = True) -> str:
    return run_adb(args, check=check).stdout.strip()


def discover_socket(package: str) -> tuple[str, int]:
    """Find the app WebView's devtools unix socket and its pid.

    Prefer the socket whose pid matches the running app (from /proc/net/unix);
    fall back to constructing the name from ``pidof`` when the socket table is
    unreadable (some devices restrict it).
    """
    pids = adb_out(["shell", "pidof", package]).split()
    if not pids:
        raise SystemExit(
            f"{package} is not running on the device. Launch Obsidian, then retry.\n"
            f"(adb shell am start -n {package}/.MainActivity)"
        )

    # Some devices restrict /proc/net/unix; tolerate a non-zero exit and fall
    # back to the pidof-constructed socket name below instead of aborting.
    unix_table = adb_out(["shell", "cat", "/proc/net/unix"], check=False)
    sockets = set(re.findall(r"@?(webview_devtools_remote_\d+)", unix_table))
    for pid in pids:
        name = f"webview_devtools_remote_{pid}"
        if name in sockets:
            return name, int(pid)
    if sockets:
        name = sorted(sockets)[0]
        pid_match = re.search(r"_(\d+)$", name)
        return name, int(pid_match.group(1)) if pid_match else int(pids[0])

    # /proc/net/unix unreadable: assume the first app pid owns the socket.
    return f"webview_devtools_remote_{pids[0]}", int(pids[0])


@contextlib.contextmanager
def cdp_forward(port: int, package: str):
    socket, pid = discover_socket(package)
    run_adb(forward_command(port, socket))
    try:
        yield pid
    finally:
        run_adb(forward_remove_command(port), check=False)


def cdp_targets(port: int) -> list[dict[str, Any]]:
    import urllib.request

    try:
        return json.load(urllib.request.urlopen(f"http://localhost:{port}/json", timeout=10))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Could not read the CDP target list on localhost:{port}: {exc}\n"
            "Is the WebView still alive? Re-run after confirming Obsidian is foregrounded."
        ) from exc


def discover_page_ws(port: int) -> tuple[str, str]:
    targets = cdp_targets(port)
    pages = [target for target in targets if target.get("type") == "page"]
    if not pages:
        raise SystemExit(f"No CDP 'page' target found. Targets: {[t.get('type') for t in targets]}")
    return pages[0]["webSocketDebuggerUrl"], pages[0].get("url", "")


# ---------- shared CDP eval ----------
async def ev(port: int, expr: str, *, timeout: float = 120.0, await_promise: bool = True) -> Any:
    import websockets

    ws_url, _url = discover_page_ws(port)
    async with websockets.connect(ws_url, max_size=None, open_timeout=20) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "allowUnsafeEvalBlockedByCSP": True,
                "userGesture": True,
            },
        }))
        while True:
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if response.get("id") != 1:
                continue
            result = response.get("result", {})
            if "exceptionDetails" in result:
                exc = result["exceptionDetails"]
                raise RuntimeError(exc.get("exception", {}).get("description") or json.dumps(exc))
            return result.get("result", {}).get("value")


async def read_runtime_state(port: int, plugin: str | None) -> dict[str, Any]:
    plugin_expr = "null" if not plugin else f"""(() => {{
        const id = {js(plugin)};
        const p = app?.plugins?.plugins?.[id];
        return {{
            id,
            manifestKnown: app?.plugins?.manifests?.[id] ?? null,
            enabled: Array.from(app?.plugins?.enabledPlugins ?? []).includes(id),
            instantiated: Boolean(p),
            loadedVersion: p?.manifest?.version ?? null,
        }};
    }})()"""
    return await ev(port, f"""(() => ({{
        vaultName: app?.vault?.getName?.() ?? null,
        vaultBasePath: app?.vault?.adapter?.basePath ?? app?.vault?.adapter?.getBasePath?.() ?? null,
        pluginsEnabled: Boolean(app?.plugins?.isEnabled?.()),
        obsidianApiVersion: window.apiVersion ?? null,
        installedPluginCount: Object.keys(app?.plugins?.plugins ?? {{}}).length,
        enabledPluginCount: Array.from(app?.plugins?.enabledPlugins ?? []).length,
        plugin: {plugin_expr},
    }}))()""")


async def enable_plugin(port: int, plugin: str) -> dict[str, Any]:
    # setEnable(true) first: Restricted Mode adds the id to the list but will not
    # instantiate the plugin until community plugins are enabled.
    return await ev(port, f"""(async () => {{
        const id = {js(plugin)};
        try {{
            if (app.plugins.setEnable) await app.plugins.setEnable(true);
            await app.plugins.loadManifests();
            if (app.plugins.plugins[id]) await app.plugins.disablePlugin(id);
            await (app.plugins.enablePluginAndSave
                ? app.plugins.enablePluginAndSave(id)
                : app.plugins.enablePlugin(id));
            return {{
                ok: true,
                enabled: Array.from(app.plugins.enabledPlugins ?? []).includes(id),
                instantiated: Boolean(app.plugins.plugins[id]),
                version: app.plugins.plugins[id]?.manifest?.version ?? null,
            }};
        }} catch (e) {{ return {{ok: false, error: String((e && e.stack) || e)}}; }}
    }})()""")


# ---------- commands ----------
async def cmd_pages(args: argparse.Namespace) -> int:
    with cdp_forward(args.port, args.bundle):
        targets = cdp_targets(args.port)
    if args.json:
        print(json.dumps(targets, indent=2, ensure_ascii=False))
    else:
        for target in targets:
            print(target.get("type"), "|", (target.get("url", "") or "")[:70])
    return 0


async def cmd_eval(args: argparse.Namespace) -> int:
    from .probes import load_probe

    expr = load_probe(args.probe) if args.probe else args.expr
    if expr is None:
        if not sys.stdin.isatty():
            expr = sys.stdin.read()
        else:
            raise SystemExit("Provide a JS expression, --probe <file|name>, or JavaScript on stdin.")

    with cdp_forward(args.port, args.bundle):
        result = await ev(args.port, expr, timeout=args.timeout, await_promise=not args.no_await)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if isinstance(result, dict) and result.get("ok") is False:
        return 2
    return 0


async def cmd_diagnose(args: argparse.Namespace) -> int:
    with cdp_forward(args.port, args.bundle) as pid:
        state = await read_runtime_state(args.port, args.plugin)
    print(json.dumps({"pid": pid, "cdpPort": args.port, "runtime": state}, indent=2, ensure_ascii=False))
    return 0


async def cmd_reload(args: argparse.Namespace) -> int:
    with cdp_forward(args.port, args.bundle):
        vault_name = await ev(args.port, "app?.vault?.getName?.() ?? null")
        guard_real_vault(vault_name or "", args, "reload")
        result = await enable_plugin(args.port, args.plugin)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


async def cmd_deploy(args: argparse.Namespace) -> int:
    from .ios import resolve_plugin_files  # same repo-path resolution as iOS

    files = resolve_plugin_files(args)
    vault_path = normalize_android_path(args.vault_path)
    vault_name = posixpath.basename(vault_path)
    guard_real_vault(vault_name, args, "deploy")

    with cdp_forward(args.port, args.bundle):
        state_before = await read_runtime_state(args.port, args.plugin)
        open_name = state_before.get("vaultName")
        if open_name != vault_name:
            raise SystemExit(
                "Refusing to deploy: the open Android vault differs from --vault-path.\n"
                f"--vault-path basename: {vault_name!r}\nOpen Obsidian vault: {open_name!r}\n"
                "Open the target vault in Obsidian Android, then rerun deploy."
            )

        target = f"{vault_path}/.obsidian/plugins/{args.plugin}"
        push_plugin_files(target, files)

        enable = await enable_plugin(args.port, args.plugin)
        runtime_after = await read_runtime_state(args.port, args.plugin)

    report = {"deployTarget": target, "enable": enable, "runtimeAfter": runtime_after}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    instantiated = (runtime_after.get("plugin") or {}).get("instantiated")
    return 0 if instantiated else 2


def android_vault_dir(root: str, vault_name: str) -> str:
    """Absolute on-device vault path from a parent root and a vault name."""
    return normalize_android_path(f"{root}/{vault_name}")


def existing_vault_files(vault_path: str) -> set[str]:
    """Relpaths (POSIX, vault-relative) of files already present under the vault.

    ``find`` exits non-zero when the vault does not exist yet; that is the normal
    first-provision case, so tolerate it and report an empty set.
    """
    listing = adb_out(["shell", "find", vault_path, "-type", "f"], check=False)
    relpaths: set[str] = set()
    for line in listing.splitlines():
        line = line.strip()
        if line.startswith(vault_path + "/"):
            relpaths.add(line[len(vault_path) + 1:])
    return relpaths


def write_device_file(device_path: str, content: bytes) -> None:
    """Write bytes to an on-device path, creating parent dirs, via a temp + adb push."""
    import tempfile

    adb_out(["shell", "mkdir", "-p", posixpath.dirname(device_path)])
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(content)
        tmp.flush()
        run_adb(["push", tmp.name, device_path])


def push_plugin_files(plugin_dir: str, files: dict[str, str]) -> None:
    """adb-push resolved plugin artifacts into an on-device plugin dir."""
    adb_out(["shell", "mkdir", "-p", plugin_dir])
    for path in files.values():
        run_adb(["push", str(path), f"{plugin_dir}/"])


async def cmd_provision(args: argparse.Namespace) -> int:
    from . import provision as prov

    vault_path = android_vault_dir(args.vault_root, args.vault)
    vault_name = posixpath.basename(vault_path)

    if args.remove:
        prov.guard_remove_vault(vault_name)
        existed = bool(adb_out(["shell", "ls", "-d", vault_path], check=False))
        if existed:
            run_adb(["shell", "rm", "-rf", vault_path])
        print(json.dumps({"action": "remove", "vaultPath": vault_path,
                          "vaultName": vault_name, "removed": existed}, indent=2))
        return 0

    prov.guard_provision_vault(
        vault_name, confirm_real=args.confirm_real_vault, test_vault=args.test_vault
    )
    files = resolve_plugin_files_for_provision(args)
    data_seed = Path(args.data).expanduser().read_bytes() if args.data else None

    skeleton = prov.vault_skeleton(args.plugin, data_seed)
    existing = existing_vault_files(vault_path)
    to_write = prov.plan_writes(skeleton, existing)
    for entry in to_write:
        write_device_file(f"{vault_path}/{entry.relpath}", entry.content)

    plugin_report: dict[str, Any] | None = None
    if files:
        plugin_dir = f"{vault_path}/.obsidian/plugins/{args.plugin}"
        push_plugin_files(plugin_dir, files)
        plugin_report = {"pluginDir": plugin_dir, "files": sorted(files)}

    report = {
        "action": "provision",
        "vaultPath": vault_path,
        "vaultName": vault_name,
        "wrote": [entry.relpath for entry in to_write],
        "skipped": sorted(existing - {entry.relpath for entry in to_write}),
        "plugin": plugin_report,
        "openVaultHint": (
            "Obsidian Android cannot switch vaults over CDP; open the vault by hand in the app "
            "(sidebar -> vault switcher -> Open folder as vault, or Manage vaults), then use "
            "`omd android reload`/`deploy`."
        ),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def resolve_plugin_files_for_provision(args: argparse.Namespace) -> dict[str, str] | None:
    """Resolve plugin artifacts only when --plugin was passed; None otherwise."""
    if not args.plugin:
        return None
    from .ios import resolve_plugin_files

    return resolve_plugin_files(args)


async def cmd_logs(args: argparse.Namespace) -> int:
    run_adb(["logcat", "-c"], check=False)
    print(f"-- streaming Android logcat (Obsidian/WebView/crash lines) for {args.seconds}s --")
    process = subprocess.Popen(
        [adb_bin(), "logcat", "-v", "time"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    tokens = ("obsidian", "webview", "chromium", "fatal", "crash")
    try:
        deadline = asyncio.get_event_loop().time() + args.seconds
        while asyncio.get_event_loop().time() < deadline:
            line = await asyncio.to_thread(process.stdout.readline)
            if not line:
                break
            if any(token in line.lower() for token in tokens):
                print(line, end="")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    return 0


# ---------- dispatch ----------
_COMMANDS = {
    "pages": cmd_pages,
    "eval": cmd_eval,
    "diagnose": cmd_diagnose,
    "reload": cmd_reload,
    "deploy": cmd_deploy,
    "provision": cmd_provision,
    "logs": cmd_logs,
}


def dispatch(args: argparse.Namespace) -> int:
    return asyncio.run(_COMMANDS[args.cmd](args))
