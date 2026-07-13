"""iOS transport: drive an Obsidian (or any WKWebView) app on a USB iPhone.

Two USB channels via pymobiledevice3 do everything:

- WebKit Web Inspector -> evaluate JS against the page (the ``app`` API), read
  state, enable/reload plugins, stream console + uncaught errors.
- AFC / house_arrest   -> read and write files in the app's documents container
  (the vault lives at ``/Documents/<vault>/``), used to push a plugin build.

Nothing here is hardcoded to a specific plugin; pass ``--plugin`` / ``--repo``.

pymobiledevice3 is imported lazily inside the connection helpers so argument
parsing and ``--help`` work without a device connected or the transport
installed. Broad ``except`` blocks that re-raise ``SystemExit`` with a concrete
next step are intentional: device errors are opaque and the hint is the value.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import posixpath
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BUNDLE = "md.obsidian"
RESULT_TOKEN = "__omd_r"
SOURCE_TOKEN = "__omd_src"
EVAL_CHUNK_SIZE = 768
INSPECTOR_CALL_TIMEOUT = 15.0

# A vault whose name contains one of these tokens is treated as a disposable
# test vault; deploy/reload against anything else needs --confirm-real-vault.
SAFE_VAULT_TOKENS = ("test", "scratch", "debug", "sandbox")


# ---------- pure helpers (no device, safe to import/unit-test) ----------
def js(value: str) -> str:
    return json.dumps(value)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def backup_root() -> Path:
    return Path(
        os.environ.get("OMD_BACKUP_DIR", Path.home() / ".obsidian-mobile-debug" / "backups")
    ).expanduser()


def backup_dir_for(device_id: str, vault_name: str, plugin: str, stamp: str) -> Path:
    return backup_root() / safe_segment(device_id) / safe_segment(vault_name) / safe_segment(plugin) / stamp


def looks_like_test_vault(name: str | None, expected: str | None = None) -> bool:
    if not name:
        return False
    if expected and name == expected:
        return True
    lowered = name.lower()
    return any(token in lowered for token in SAFE_VAULT_TOKENS)


def guard_real_vault(
    vault_name: str, args: argparse.Namespace, operation: str,
    identity: dict[str, Any] | None = None,
) -> None:
    if getattr(args, "confirm_real_vault", False):
        return
    if looks_like_test_vault(vault_name, getattr(args, "test_vault", None)):
        return
    identity_line = ""
    if identity is not None:
        from .provision import describe_vault_identity

        identity_line = f"\nVault identity: {describe_vault_identity(identity)}"
    raise SystemExit(
        f"Refusing to {operation} against vault {vault_name!r}: it does not look like a test "
        f"vault (name contains none of {SAFE_VAULT_TOKENS}) and this touches a real Obsidian "
        f"vault.{identity_line}\nRe-run with --confirm-real-vault to proceed, or --test-vault "
        f"{vault_name!r} to whitelist this name."
    )


def resolve_plugin_files(args: argparse.Namespace) -> dict[str, str]:
    """Map on-device filename -> local path. styles.css is included only if present."""
    if args.main:
        main = args.main
    elif args.repo:
        root = os.path.expanduser(args.repo)
        main = os.path.join(root, "main.js")
        if not os.path.exists(main):
            main = os.path.join(root, "build", "main.js")
    else:
        raise SystemExit("deploy needs --repo or explicit --main/--manifest")

    manifest = args.manifest or (
        os.path.join(os.path.expanduser(args.repo), "manifest.json") if args.repo else None
    )
    if not manifest:
        raise SystemExit("deploy needs --manifest (or --repo)")

    files = {"main.js": main, "manifest.json": manifest}
    styles = args.styles or (
        os.path.join(os.path.expanduser(args.repo), "styles.css") if args.repo else None
    )
    if styles and os.path.isfile(styles):
        files["styles.css"] = styles
    for name, path in files.items():
        if not os.path.isfile(path):
            raise SystemExit(f"missing build file for {name}: {path}")
    return files


def collect_local_file_manifest(local_dir: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        files[path.relative_to(local_dir).as_posix()] = {"bytes": len(data), "sha256": sha256(data)}
    return files


# ---------- lazy device connection ----------
async def _create_lockdown() -> Any:
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
    except ImportError as exc:
        raise SystemExit(
            "pymobiledevice3 is not installed. Install this tool with its dependencies "
            "(`uv tool install .`) or run inside `uv run --with pymobiledevice3 ...`."
        ) from exc
    try:
        return await create_using_usbmux()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Could not connect to an iPhone over usbmux: {exc}\n"
            "Unlock the phone, trust this Mac, and confirm it is connected over USB."
        ) from exc


def device_id(lockdown: Any) -> str:
    return str(
        getattr(lockdown, "udid", None) or getattr(lockdown, "identifier", None) or "usb-device"
    )


@contextlib.asynccontextmanager
async def inspector_session_unlocked(lockdown: Any, bundle: str):
    """One inspector session without the cross-process lock.

    Only for callers that already hold the inspector lock themselves (verify
    holds it once across several sessions; the flock is not reentrant, so
    nesting the locked variant would contend against its own process).
    """
    from pymobiledevice3.services.webinspector import WebinspectorService

    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()
    try:
        async with inspector:
            target, session = await open_session(inspector, bundle)
            yield target, session
    finally:
        await inspector.close()


@contextlib.asynccontextmanager
async def inspector_session(lockdown: Any, bundle: str):
    from .lock import inspector_lock

    # Acquired before WebinspectorService.connect(): a second OMD process would
    # otherwise hang on the shared inspector session until the first exits.
    with inspector_lock(device_id(lockdown), bundle):
        async with inspector_session_unlocked(lockdown, bundle) as pair:
            yield pair


def page_matches_bundle(page: Any, bundle: str) -> bool:
    """Whether an inspector page belongs to the requested bundle.

    Obsidian's iOS inspector page does not always report bundle == md.obsidian,
    so fall back to a name match - but only when targeting the default bundle,
    so an explicit --bundle for another app never silently hits Obsidian.
    """
    if page.application.bundle == bundle:
        return True
    if bundle != DEFAULT_BUNDLE:
        return False
    name = (page.application.name or "").lower()
    return "obsidian" in name


async def open_session(inspector: Any, bundle: str) -> tuple[Any, Any]:
    pages = await inspector.get_open_application_pages(timeout=3)
    target = next((page for page in pages if page_matches_bundle(page, bundle)), None)
    if target is None:
        raise SystemExit(
            f"No inspectable page for bundle {bundle!r}. Unlock the phone, open the app, and "
            "enable Settings > Apps > Safari > Advanced > Web Inspector.\n"
            f"Pages seen: {[str(page) for page in pages]}"
        )
    session = await inspector.inspector_session(target.application, target.page)
    await session.runtime_enable()
    return target, session


# ---------- robust async-aware eval (base64 chunk + stash-and-poll) ----------
async def _runtime_evaluate(session: Any, expression: str, timeout: float = INSPECTOR_CALL_TIMEOUT) -> Any:
    try:
        return await asyncio.wait_for(
            session.runtime_evaluate(expression, return_by_value=True), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"Web Inspector Runtime.evaluate timed out after {timeout}s: {expression[:120]}"
        ) from exc


async def ev(session: Any, expr: str, timeout: float = 30.0) -> Any:
    """Evaluate JS against the page, awaiting promises and surviving WebKit quirks.

    The bundled pymobiledevice3 throws ``KeyError: 'preview'`` on a JS ``null``
    and does not reliably await promises, so the expression is base64-chunked
    onto the page, wrapped in an async runner, and the JSON-stringified result is
    stashed on ``window.__omd_r`` and polled. This handles both bare expressions
    and multi-statement probe files (they are wrapped in ``return await (...)``).
    """
    encoded = base64.b64encode(expr.encode("utf-8")).decode("ascii")
    await _runtime_evaluate(session, f"window.{SOURCE_TOKEN}='';0")
    for index in range(0, len(encoded), EVAL_CHUNK_SIZE):
        chunk = encoded[index:index + EVAL_CHUNK_SIZE]
        await _runtime_evaluate(session, f"window.{SOURCE_TOKEN}+={js(chunk)};0")

    kickoff = (
        f"(()=>{{const __bytes=Uint8Array.from(atob(window.{SOURCE_TOKEN}),c=>c.charCodeAt(0));"
        f"const __source=new TextDecoder('utf-8').decode(__bytes);"
        f"window.{RESULT_TOKEN}=undefined;"
        f"const __fmt=e=>String(e&&e.message?e.message+'\\n'+(e.stack||''):(e&&e.stack)||e);"
        f"(async()=>{{try{{const __runner=new Function('return (async()=>{{return await ('+__source+');}})()');"
        f"window.{RESULT_TOKEN}={{ok:JSON.stringify(await __runner())}}}}"
        f"catch(e){{window.{RESULT_TOKEN}={{err:__fmt(e)}}}}}})();return 0}})()"
    )
    await _runtime_evaluate(session, kickoff)

    waited = 0.0
    poll = f"JSON.stringify(window.{RESULT_TOKEN}===undefined?null:window.{RESULT_TOKEN})"
    while waited < timeout:
        result = await _runtime_evaluate(session, poll)
        if result and result != "null":
            obj = json.loads(result)
            if "err" in obj:
                raise RuntimeError(obj["err"])
            value = obj.get("ok")
            if value is None:
                return None
            try:
                return json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return value
        await asyncio.sleep(0.1)
        waited += 0.1
    raise TimeoutError(f"eval timed out after {timeout}s: {expr[:120]}")


# ---------- runtime state / plugin lifecycle ----------
async def read_vault_identity(session: Any) -> dict[str, Any]:
    """Identity of the vault Obsidian has open: display name + backing storage.

    The name alone is ambiguous (two vaults can share it); the recorded
    localStorage path pins down which storage backs the open vault.
    """
    from . import provision as prov

    raw = await ev(session, f"""(() => ({{
        name: app.vault?.getName?.() ?? null,
        path: {prov.CURRENT_SELECTED_VAULT_JS},
    }}))()""") or {}
    return prov.vault_identity(raw.get("name"), raw.get("path"))


async def read_runtime_state(session: Any, plugin: str | None) -> dict[str, Any]:
    plugin_expr = "null" if not plugin else f"""(() => {{
        const id = {js(plugin)};
        const p = app.plugins.plugins[id];
        return {{
            id,
            manifestKnown: app.plugins.manifests?.[id] ?? null,
            enabled: Array.from(app.plugins.enabledPlugins ?? []).includes(id),
            instantiated: Boolean(p),
            loadedVersion: p?.manifest?.version ?? null,
        }};
    }})()"""
    return await ev(session, f"""(() => ({{
        vaultName: app.vault?.getName?.() ?? null,
        configDir: app.vault?.configDir ?? null,
        platform: app.isMobile ? "mobile" : "desktop",
        obsidianApiVersion: (typeof apiVersion !== "undefined" ? apiVersion : (window.apiVersion ?? null)),
        installedPluginCount: Object.keys(app.plugins?.plugins ?? {{}}).length,
        enabledPluginCount: Array.from(app.plugins?.enabledPlugins ?? []).length,
        plugin: {plugin_expr},
    }}))()""")


async def enable_plugin(session: Any, plugin: str) -> dict[str, Any]:
    return await ev(session, f"""(async () => {{
        const id = {js(plugin)};
        try {{
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


# ---------- AFC (house_arrest documents container) ----------
async def afc_open(lockdown: Any, bundle: str) -> Any:
    from pymobiledevice3.services.house_arrest import HouseArrestService

    return await HouseArrestService.create(lockdown, bundle, documents_only=True)


async def afc_find_vault(afc: Any, prefer: str | None) -> tuple[str, list[str]]:
    candidates: list[str] = []
    for name in await afc.listdir("/Documents"):
        if name in (".", "..", ""):
            continue
        try:
            if await afc.exists(f"/Documents/{name}/.obsidian"):
                candidates.append(name)
        except Exception:  # noqa: BLE001
            continue

    if prefer and prefer != "auto":
        if prefer in candidates:
            return f"/Documents/{prefer}", candidates
        raise SystemExit(
            f"Vault {prefer!r} was not found under /Documents. "
            f"Vaults with .obsidian: {candidates or '(none)'}"
        )
    if len(candidates) == 1:
        return f"/Documents/{candidates[0]}", candidates
    if not candidates:
        raise SystemExit("No Obsidian vault (.obsidian) found under the app's Documents container.")
    raise SystemExit(f"Multiple vaults found; pass --vault one of: {candidates}")


def plugin_dir_for(vault_path: str, plugin: str) -> str:
    return f"{vault_path}/.obsidian/plugins/{plugin}"


async def afc_put_verified(afc: Any, remote_path: str, data: bytes) -> dict[str, Any]:
    await afc.set_file_contents(remote_path, data)
    remote = bytes(await afc.get_file_contents(remote_path))
    return {
        "bytes": len(remote),
        "wantBytes": len(data),
        "sha256": sha256(remote),
        "wantSha256": sha256(data),
        "ok": len(remote) == len(data) and sha256(remote) == sha256(data),
    }


async def collect_remote_file_manifest(afc: Any, remote_dir: str) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    async for dirpath, _dirnames, filenames in afc.walk(remote_dir):
        for filename in filenames:
            remote_path = posixpath.join(dirpath, filename)
            relpath = posixpath.relpath(remote_path, remote_dir)
            data = bytes(await afc.get_file_contents(remote_path))
            files[relpath] = {"bytes": len(data), "sha256": sha256(data)}
    return files


async def backup_existing_plugin(
    afc: Any, remote_plugin_dir: str, dev_id: str, vault_name: str, plugin: str, state: Any
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir_for(dev_id, vault_name, plugin, stamp)
    target.mkdir(parents=True, exist_ok=False)

    exists = await afc.exists(remote_plugin_dir)
    file_manifest: dict[str, dict[str, Any]] = {}
    if exists:
        file_manifest = await collect_remote_file_manifest(afc, remote_plugin_dir)
        await afc.pull(remote_plugin_dir, str(target), progress_bar=False)
        local_plugin_dir = target / posixpath.basename(remote_plugin_dir)
        if collect_local_file_manifest(local_plugin_dir) != file_manifest:
            raise SystemExit(
                f"Pre-deploy backup verification failed; refusing to write to the phone. Backup: {target}"
            )

    manifest = {
        "createdAt": stamp,
        "deviceId": dev_id,
        "pluginId": plugin,
        "vaultName": vault_name,
        "remotePluginDir": remote_plugin_dir,
        "remotePluginDirExisted": exists,
        "fileManifest": file_manifest,
        "stateBeforeDeploy": state,
        "note": "Created by obsidian-mobile-debug (omd) before writing to an Obsidian iOS vault.",
    }
    (target / "backup-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def iter_backups() -> list[Path]:
    root = backup_root()
    if not root.exists():
        return []
    manifests = root.glob("*/*/*/*/backup-manifest.json")
    return sorted((m.parent for m in manifests), key=lambda p: p.stat().st_mtime)


def latest_backup() -> Path:
    backups = iter_backups()
    if not backups:
        raise SystemExit(f"No backups found below {backup_root()}")
    return backups[-1]


# ---------- commands ----------
async def cmd_pages(lockdown: Any, args: argparse.Namespace) -> int:
    from pymobiledevice3.services.webinspector import WebinspectorService

    from .lock import inspector_lock

    with inspector_lock(device_id(lockdown), args.bundle):
        inspector = WebinspectorService(lockdown=lockdown)
        await inspector.connect()
        try:
            async with inspector:
                pages = await inspector.get_open_application_pages(timeout=3)
                if args.json:
                    print(json.dumps([str(page) for page in pages], indent=2, ensure_ascii=False))
                else:
                    for page in pages:
                        print(page)
        finally:
            await inspector.close()
    return 0


async def cmd_eval(lockdown: Any, args: argparse.Namespace) -> int:
    from .probes import load_probe

    expr = load_probe(args.probe) if args.probe else args.expr
    if expr is None:
        if not sys.stdin.isatty():
            expr = sys.stdin.read()
        else:
            raise SystemExit("Provide a JS expression, --probe <file|name>, or JavaScript on stdin.")

    async with inspector_session(lockdown, args.bundle) as (_target, session):
        result = await ev(session, expr, timeout=args.timeout)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if isinstance(result, dict) and result.get("ok") is False:
        return 2
    return 0


async def cmd_command(lockdown: Any, args: argparse.Namespace) -> int:
    async with inspector_session(lockdown, args.bundle) as (_target, session):
        result = await ev(
            session,
            f"(() => ({{executed: app.commands.executeCommandById({js(args.command_id)})}}))()",
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


async def cmd_diagnose(lockdown: Any, args: argparse.Namespace) -> int:
    from . import provision as prov

    report: dict[str, Any] = {}
    async with inspector_session(lockdown, args.bundle) as (target, session):
        report["inspectorTarget"] = str(target)
        report["vaultIdentity"] = await read_vault_identity(session)
        report["runtime"] = await read_runtime_state(session, args.plugin)

    if args.plugin:
        afc = await afc_open(lockdown, args.bundle)
        try:
            vault_path, vaults = await afc_find_vault(afc, args.vault)
            pdir = plugin_dir_for(vault_path, args.plugin)
            report["afc"] = {
                "vaultPath": vault_path,
                "vaults": vaults,
                # Whether this AFC candidate is the vault Obsidian actually has
                # open: same-name vaults on different storage make the name
                # alone misleading.
                "correspondsToRuntimeVault": prov.afc_vault_corresponds(
                    report["vaultIdentity"].get("selectedVaultPath"),
                    vault_path.rsplit("/", 1)[-1],
                ),
                "pluginDir": pdir,
                "pluginDirExists": await afc.exists(pdir),
                "pluginFiles": await afc.listdir(pdir) if await afc.exists(pdir) else [],
            }
        finally:
            await afc.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


async def cmd_reload(lockdown: Any, args: argparse.Namespace) -> int:
    async with inspector_session(lockdown, args.bundle) as (_target, session):
        identity = await read_vault_identity(session)
        guard_real_vault(identity.get("vaultName") or "", args, "reload", identity)
        result = await enable_plugin(session, args.plugin)
    result["vaultIdentity"] = identity
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


async def cmd_deploy(lockdown: Any, args: argparse.Namespace) -> int:
    from . import provision as prov

    files = resolve_plugin_files(args)

    async with inspector_session(lockdown, args.bundle) as (_target, session):
        identity = await read_vault_identity(session)
        state_before = await read_runtime_state(session, args.plugin)

        afc = await afc_open(lockdown, args.bundle)
        try:
            vault_path, _vaults = await afc_find_vault(afc, args.vault)
            vault_name = vault_path.rsplit("/", 1)[-1]
            guard_real_vault(vault_name, args, "deploy", identity)

            # Name equality alone is not identity: a same-name iCloud/external
            # vault would pass it while AFC writes to the app-container twin
            # Obsidian is not running. Require the open vault to be the
            # app-container vault this AFC path actually backs.
            if not prov.afc_vault_corresponds(identity.get("selectedVaultPath"), vault_name):
                raise SystemExit(
                    "Refusing to deploy: the AFC target vault is not the vault open in Obsidian.\n"
                    f"AFC target vault: {vault_name!r} (app-container, {vault_path})\n"
                    f"Open Obsidian vault: {prov.describe_vault_identity(identity)}\n"
                    "Open the app-container target vault on the phone, then rerun deploy."
                )

            pdir = plugin_dir_for(vault_path, args.plugin)
            dev_id = device_id(lockdown)
            report: dict[str, Any] = {"deployTarget": pdir, "vaultIdentity": identity}

            if not args.no_backup:
                report["backup"] = str(
                    await backup_existing_plugin(afc, pdir, dev_id, vault_name, args.plugin, state_before)
                )

            try:
                await afc.makedirs(pdir)
            except Exception:  # noqa: BLE001
                pass

            pushed: dict[str, Any] = {}
            for name, path in files.items():
                pushed[name] = await afc_put_verified(afc, f"{pdir}/{name}", Path(path).read_bytes())
            pushed[".hotreload"] = await afc_put_verified(afc, f"{pdir}/.hotreload", b"")
            report["pushed"] = pushed
            if not all(entry.get("ok") for entry in pushed.values()):
                print(json.dumps(report, indent=2, ensure_ascii=False))
                raise SystemExit("At least one pushed file failed byte verification.")
        finally:
            await afc.close()

        report["enable"] = await enable_plugin(session, args.plugin)
        report["runtimeAfter"] = await read_runtime_state(session, args.plugin)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    instantiated = (report.get("runtimeAfter", {}).get("plugin") or {}).get("instantiated")
    return 0 if instantiated else 2


async def cmd_restore(lockdown: Any, args: argparse.Namespace) -> int:
    source = Path(args.backup).expanduser() if args.backup else latest_backup()
    manifest_path = source / "backup-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"Backup manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    remote_plugin_dir = manifest["remotePluginDir"]
    remote_parent = remote_plugin_dir.rsplit("/", 1)[0]
    plugin = manifest["pluginId"]
    vault_name = manifest.get("vaultName")
    local_plugin_dir = source / posixpath.basename(remote_plugin_dir)

    expected_device = manifest.get("deviceId")
    current_device = device_id(lockdown)
    if expected_device and expected_device != current_device and not args.force:
        raise SystemExit(
            "Refusing to restore a backup from a different device without --force.\n"
            f"Backup device: {expected_device}\nConnected device: {current_device}"
        )

    afc = await afc_open(lockdown, args.bundle)
    try:
        vault_path, _vaults = await afc_find_vault(afc, vault_name)
        expected_dir = plugin_dir_for(vault_path, plugin)
        if remote_plugin_dir != expected_dir:
            raise SystemExit(
                "Backup plugin path does not match the connected device's current vault path.\n"
                f"backup={remote_plugin_dir} current={expected_dir}"
            )
        if await afc.exists(remote_plugin_dir):
            undeleted = await afc.rm(remote_plugin_dir, force=True)
            if undeleted:
                raise SystemExit(f"Could not remove current remote plugin dir: {undeleted}")

        if manifest.get("remotePluginDirExisted"):
            if not local_plugin_dir.is_dir():
                raise SystemExit(f"Backup plugin folder not found: {local_plugin_dir}")
            await afc.push(str(local_plugin_dir), remote_parent)
            if await collect_remote_file_manifest(afc, remote_plugin_dir) != collect_local_file_manifest(local_plugin_dir):
                raise SystemExit("Restore verification failed; remote folder does not match the backup.")
            print(f"Restored {local_plugin_dir} -> {remote_plugin_dir}")
        else:
            print(f"Removed {remote_plugin_dir}; backup recorded no plugin dir existed before deploy.")
    finally:
        await afc.close()

    if args.no_reload:
        return 0
    async with inspector_session(lockdown, args.bundle) as (_target, session):
        result = await enable_plugin(session, plugin)
    print(json.dumps({"reload": result}, indent=2, ensure_ascii=False))
    return 0


async def existing_vault_files_afc(afc: Any, vault_path: str) -> set[str]:
    """Relpaths (vault-relative, POSIX) already present under an AFC vault dir."""
    if not await afc.exists(vault_path):
        return set()
    manifest = await collect_remote_file_manifest(afc, vault_path)
    return set(manifest)


async def provision_scratch_vault(
    afc: Any, vault_name: str, plugin: str | None,
    files: dict[str, str] | None, data_seed: bytes | None,
) -> dict[str, Any]:
    """Write the vault skeleton (and plugin artifacts, hash-verified) over AFC.

    Shared by `provision` and `verify`. Idempotency lives in
    ``prov.plan_writes``; every plugin file push is byte/hash verified.
    """
    from . import provision as prov

    vault_path = f"{prov.IOS_DOCUMENTS_ROOT}/{vault_name}"
    skeleton = prov.vault_skeleton(plugin, data_seed)
    existing = await existing_vault_files_afc(afc, vault_path)
    to_write = prov.plan_writes(skeleton, existing)
    for entry in to_write:
        full = f"{vault_path}/{entry.relpath}"
        await afc.makedirs(posixpath.dirname(full))
        await afc.set_file_contents(full, entry.content)

    plugin_report: dict[str, Any] | None = None
    if files:
        pdir = plugin_dir_for(vault_path, plugin)
        await afc.makedirs(pdir)
        pushed = {
            name: await afc_put_verified(afc, f"{pdir}/{name}", Path(path).read_bytes())
            for name, path in files.items()
        }
        plugin_report = {"pluginDir": pdir, "pushed": pushed}

    return {
        "vaultPath": vault_path,
        "wrote": [entry.relpath for entry in to_write],
        "skipped": sorted(existing - {entry.relpath for entry in to_write}),
        "plugin": plugin_report,
    }


async def cmd_provision(lockdown: Any, args: argparse.Namespace) -> int:
    from . import provision as prov

    vault_name, vault_name_source = prov.resolve_vault_name(
        args.vault, getattr(args, "plugin", None)
    )
    vault_path = f"{prov.IOS_DOCUMENTS_ROOT}/{vault_name}"

    afc = await afc_open(lockdown, args.bundle)
    try:
        if args.remove:
            prov.guard_remove_vault(vault_name)
            existed = await afc.exists(vault_path)
            if existed:
                undeleted = await afc.rm(vault_path, force=True)
                if undeleted:
                    raise SystemExit(f"Could not fully remove {vault_path}: {undeleted}")
            print(json.dumps({"action": "remove", "vaultPath": vault_path,
                              "vaultName": vault_name, "vaultNameSource": vault_name_source,
                              "removed": existed}, indent=2))
            return 0

        prov.guard_provision_vault(
            vault_name, confirm_real=args.confirm_real_vault, test_vault=args.test_vault
        )
        files = resolve_plugin_files(args) if args.plugin else None
        data_seed = Path(args.data).expanduser().read_bytes() if args.data else None
        provisioned = await provision_scratch_vault(afc, vault_name, args.plugin, files, data_seed)
        plugin_report = provisioned["plugin"]
    finally:
        await afc.close()

    opened: dict[str, Any] | None = None
    if args.open:
        # Derive the sandbox-absolute vault path from the currently-open vault
        # (AFC's /Documents view does not expose it), then register + select +
        # reload via the same localStorage mechanism as Android.
        async with inspector_session(lockdown, args.bundle) as (_target, session):
            current = await ev(session, prov.CURRENT_SELECTED_VAULT_JS)
            open_path = prov.derive_sibling_vault_path(current, vault_name)
            opened = await ev(session, prov.open_vault_js(open_path))

    report = {
        "action": "provision",
        "vaultPath": vault_path,
        "vaultName": vault_name,
        "vaultNameSource": vault_name_source,
        # AFC house_arrest can only write inside the app's own sandbox, so a
        # provisioned vault is app-container-backed by construction. A
        # same-name vault Obsidian may have open could still be iCloud or
        # external - `diagnose` reports which one is actually selected.
        "storageKind": "app-container",
        "wrote": provisioned["wrote"],
        "skipped": provisioned["skipped"],
        "plugin": plugin_report,
        "opened": opened,
        "openVaultHint": prov.open_hint(args.open, args.plugin, "ios"),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if plugin_report and not all(entry.get("ok") for entry in plugin_report["pushed"].values()):
        raise SystemExit("At least one pushed plugin file failed byte verification.")
    return 0


ERROR_HOOK_JS = """((w) => {
    if (w.__omdHooked) return "already";
    w.__omdHooked = true;
    w.addEventListener("error", (e) => console.error("[onerror]", e.message,
        (e.filename || "") + ":" + (e.lineno || ""), e.error && e.error.stack || ""));
    w.addEventListener("unhandledrejection", (e) => console.error("[unhandledrejection]",
        e.reason && (e.reason.stack || e.reason) || e.reason));
    return "hooked";
})(window)"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def install_console_capture(session: Any, emit: Any) -> None:
    """Route Console.messageAdded events to ``emit(message_dict)``.

    Replaces pymobiledevice3's default console handlers on this session: they
    log only ``message.text`` (the first console argument) and drop the
    ``parameters`` array holding the rest. Install before ``console_enable()``
    so the replay of buffered messages already goes through ``emit``. A repeat
    notification re-emits the previous message with its updated count.
    """
    last_message: dict[str, Any] | None = None

    def on_added(response: dict[str, Any]) -> None:
        nonlocal last_message
        last_message = response["params"]["message"]
        emit(last_message)

    def on_repeat(response: dict[str, Any]) -> None:
        if last_message is None:
            return
        repeated = dict(last_message)
        count = response.get("params", {}).get("count")
        if count:
            repeated["repeatCount"] = count
        emit(repeated)

    session.response_methods["Console.messageAdded"] = on_added
    session.response_methods["Console.messageRepeatCountUpdated"] = on_repeat


async def cmd_logs(lockdown: Any, args: argparse.Namespace) -> int:
    from .console_fmt import format_console_event, format_console_line

    async with inspector_session(lockdown, args.bundle) as (_target, session):
        await ev(session, ERROR_HOOK_JS)

        def emit(message: dict[str, Any]) -> None:
            event = format_console_event(message, utc_now_iso())
            print(json.dumps(event, ensure_ascii=False) if args.json
                  else format_console_line(event), flush=True)

        install_console_capture(session, emit)
        if not args.json:
            print(f"-- streaming console + uncaught errors for {args.seconds}s --")
        await session.console_enable()
        await asyncio.sleep(args.seconds)
    return 0


def cmd_backups(args: argparse.Namespace) -> int:
    root = backup_root()
    entries = []
    for backup in iter_backups():
        data = json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8"))
        entries.append({
            "path": str(backup),
            "createdAt": data.get("createdAt"),
            "pluginId": data.get("pluginId"),
            "vaultName": data.get("vaultName"),
            "remotePluginDirExisted": data.get("remotePluginDirExisted"),
        })
    print(json.dumps({"backupRoot": str(root), "backups": entries}, indent=2, ensure_ascii=False))
    return 0


async def cmd_verify(lockdown: Any, args: argparse.Namespace) -> int:
    from .verify import cmd_verify_ios

    return await cmd_verify_ios(lockdown, args)


# ---------- dispatch ----------
_ASYNC_COMMANDS = {
    "pages": cmd_pages,
    "eval": cmd_eval,
    "command": cmd_command,
    "diagnose": cmd_diagnose,
    "reload": cmd_reload,
    "deploy": cmd_deploy,
    "provision": cmd_provision,
    "restore": cmd_restore,
    "verify": cmd_verify,
    "logs": cmd_logs,
}


def dispatch(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "backups":
        return cmd_backups(args)

    async def _run() -> int:
        lockdown = await _create_lockdown()
        return await _ASYNC_COMMANDS[args.cmd](lockdown, args)

    return asyncio.run(_run())
