"""``omd <platform> verify``: the complete mobile plugin verification loop.

One command that composes the existing primitives - diagnose, provision,
vault switch, reload, probes, console capture, restore, cleanup - into the
standard proof run, holding ONE owned inspector/CDP transport so log capture
and probe execution never contend (issue #6's lock is taken once for the
whole run on iOS).

Flow (both platforms, platform-specific evidence where they differ):

1. diagnose the runtime and record the original vault identity;
2. provision a namespaced scratch vault (plugin artifacts hash-verified on
   iOS, best-effort sha256 on Android);
3. switch Obsidian into the scratch vault and wait for the reload;
4. enable the plugin and assert it is enabled + instantiated;
5. run each probe while capturing every console argument on the same session;
6. optionally keep capturing logs for --logs-seconds;
7. restore the original vault (default; --keep-vault skips) and, with
   --cleanup, remove the scratch vault after a successful restore;
8. print ONE structured JSON summary.

Exit codes: 0 = every assertion passed; 2 = an assertion failed (probe
returned ok:false or threw, plugin not instantiated, artifact verification
failed); 1 = transport/tool failure (SystemExit with a hint, as everywhere
else in omd). On a transport failure after the vault switch, a best-effort
restore still runs and the partial summary is printed before exiting.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

DEFAULT_PROBES = ["core_smoke"]
VAULT_SWITCH_TIMEOUT = 90.0
RECONNECT_POLL_SECONDS = 2.0


def resolve_probes(args: argparse.Namespace) -> list[tuple[str, str]]:
    """[(reference, JS source)] for every requested probe, defaulting to core_smoke."""
    from .probes import load_probe

    refs = args.probe or list(DEFAULT_PROBES)
    return [(ref, load_probe(ref)) for ref in refs]


def probe_passed(value: Any) -> bool:
    """A probe fails only by returning {ok: false} (same contract as eval)."""
    return not (isinstance(value, dict) and value.get("ok") is False)


def summarize_assertions(summary: dict[str, Any], failures: list[str]) -> None:
    summary["assertions"] = {"passed": not failures, "failures": failures}


def emit_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, indent=2, ensure_ascii=False))


# ---------- iOS ----------
class _VaultNotOpen(Exception):
    def __init__(self, identity: dict[str, Any]):
        self.identity = identity


async def _with_vault_session(
    lockdown: Any, bundle: str, expected_vault: str, body: Any,
    timeout: float = VAULT_SWITCH_TIMEOUT,
) -> Any:
    """Run ``body(session, identity)`` in a session where ``expected_vault`` is open.

    Obsidian's vault switch is a page reload: the inspector page dies and a
    new one appears seconds later, so sessions are reopened (and mid-reload
    connect errors retried) until the expected vault reports open. The
    caller must already hold the inspector lock. Errors raised by ``body``
    itself propagate - only pre-match connect/read failures are retried.
    """
    from . import ios

    deadline = time.monotonic() + timeout
    attempts: list[str] = []
    while True:
        matched = False
        try:
            async with ios.inspector_session_unlocked(lockdown, bundle) as (_target, session):
                identity = await ios.read_vault_identity(session)
                if identity.get("vaultName") != expected_vault:
                    raise _VaultNotOpen(identity)
                matched = True
                return await body(session, identity)
        except _VaultNotOpen as exc:
            attempts.append(f"open vault is {exc.identity.get('vaultName')!r}")
        except (SystemExit, TimeoutError, RuntimeError, OSError, ConnectionError) as exc:
            if matched:
                raise
            attempts.append(str(exc).split("\n")[0])
        if time.monotonic() > deadline:
            raise SystemExit(
                f"Vault {expected_vault!r} did not open within {timeout:.0f}s.\n"
                f"Attempts: {attempts[-5:]}\n"
                "The app may be showing the vault chooser - check the phone screen."
            )
        await asyncio.sleep(RECONNECT_POLL_SECONDS)


async def _ios_switch_vault(
    lockdown: Any, bundle: str, open_path: str, trust_plugins: bool = False,
) -> dict[str, Any]:
    from . import ios, provision as prov

    async with ios.inspector_session_unlocked(lockdown, bundle) as (_target, session):
        return await ios.ev(session, prov.open_vault_js(open_path, trust_plugins=trust_plugins))


def _ios_runtime_phase(args: argparse.Namespace, probes: list[tuple[str, str]]):
    """Body run inside the scratch-vault session: enable, probe, capture."""
    from . import ios
    from .console_fmt import format_console_event

    async def body(session: Any, identity: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        ios.install_console_capture(
            session, lambda message: events.append(format_console_event(message, ios.utc_now_iso()))
        )
        await session.console_enable()
        await ios.ev(session, ios.ERROR_HOOK_JS)

        phase: dict[str, Any] = {"vaultIdentity": identity}
        phase["enable"] = await ios.enable_plugin(session, args.plugin)
        phase["runtime"] = await ios.read_runtime_state(session, args.plugin)

        probe_reports: list[dict[str, Any]] = []
        for ref, source in probes:
            first_event = len(events)
            started = time.monotonic()
            report: dict[str, Any] = {"probe": ref}
            try:
                value = await ios.ev(session, source, timeout=args.probe_timeout)
                report["ok"] = probe_passed(value)
                report["result"] = value
            except (RuntimeError, TimeoutError) as exc:
                report["ok"] = False
                report["error"] = str(exc)
            report["durationMs"] = round((time.monotonic() - started) * 1000)
            report["console"] = events[first_event:]
            probe_reports.append(report)
        phase["probes"] = probe_reports

        if args.logs_seconds:
            first_event = len(events)
            await asyncio.sleep(args.logs_seconds)
            phase["logs"] = {"seconds": args.logs_seconds, "console": events[first_event:]}
        return phase

    return body


async def cmd_verify_ios(lockdown: Any, args: argparse.Namespace) -> int:
    from . import ios, provision as prov
    from .lock import inspector_lock

    probes = resolve_probes(args)
    files = ios.resolve_plugin_files(args)
    data_seed = Path(args.data).expanduser().read_bytes() if args.data else None
    vault_name, vault_name_source = prov.resolve_vault_name(args.vault, args.plugin)
    prov.guard_provision_vault(
        vault_name, confirm_real=args.confirm_real_vault, test_vault=args.test_vault
    )
    if args.cleanup and args.keep_vault:
        raise SystemExit("--cleanup and --keep-vault contradict each other; pick one.")

    summary: dict[str, Any] = {
        "action": "verify",
        "platform": "ios",
        "device": ios.device_id(lockdown),
        "plugin": args.plugin,
        "vault": {"name": vault_name, "source": vault_name_source, "storageKind": "app-container"},
    }
    failures: list[str] = []
    switched = False
    original_identity: dict[str, Any] | None = None

    with inspector_lock(ios.device_id(lockdown), args.bundle):
        try:
            # 1. diagnose + derive the scratch vault's openable path up front,
            # so an iCloud/external original vault fails BEFORE any mutation.
            async with ios.inspector_session_unlocked(lockdown, args.bundle) as (target, session):
                original_identity = await ios.read_vault_identity(session)
                summary["diagnose"] = {
                    "inspectorTarget": str(target),
                    "originalVault": original_identity,
                    "runtime": await ios.read_runtime_state(session, args.plugin),
                }
            scratch_open_path = prov.derive_sibling_vault_path(
                original_identity.get("selectedVaultPath"), vault_name
            )

            # 2. provision (skeleton + hash-verified plugin artifacts).
            afc = await ios.afc_open(lockdown, args.bundle)
            try:
                provisioned = await ios.provision_scratch_vault(
                    afc, vault_name, args.plugin, files, data_seed
                )
            finally:
                await afc.close()
            summary["provision"] = provisioned
            pushed = (provisioned.get("plugin") or {}).get("pushed") or {}
            if not pushed or not all(entry.get("ok") for entry in pushed.values()):
                failures.append("plugin artifact byte/hash verification failed")
                raise _AbortAssertions

            # 3. switch into the scratch vault; 4-6. runtime phase in ONE session.
            # trust_plugins pre-clears the Restricted Mode prompt so the run is
            # hands-off; restore below deliberately never sets it.
            summary["open"] = await _ios_switch_vault(
                lockdown, args.bundle, scratch_open_path, trust_plugins=True
            )
            switched = True
            phase = await _with_vault_session(
                lockdown, args.bundle, vault_name, _ios_runtime_phase(args, probes)
            )
            summary.update(phase)

            plugin_state = (phase.get("runtime") or {}).get("plugin") or {}
            if not plugin_state.get("enabled") or not plugin_state.get("instantiated"):
                failures.append(
                    f"plugin {args.plugin!r} is not enabled+instantiated in the scratch vault"
                )
            failures.extend(
                f"probe {report['probe']!r} failed"
                for report in phase.get("probes", []) if not report.get("ok")
            )
        except _AbortAssertions:
            pass
        except BaseException:
            # Transport/tool failure: restore best-effort, print the partial
            # summary, then let the error propagate (exit 1).
            summary["error"] = "transport or tool failure - see stderr"
            await _ios_restore_and_cleanup(
                lockdown, args, summary, failures, switched, original_identity, cleanup=False
            )
            summarize_assertions(summary, failures)
            emit_summary(summary)
            raise

        await _ios_restore_and_cleanup(
            lockdown, args, summary, failures, switched, original_identity, cleanup=args.cleanup
        )

    summarize_assertions(summary, failures)
    emit_summary(summary)
    return 0 if not failures else 2


class _AbortAssertions(Exception):
    """Stop the verification early on a failed assertion (not a tool error)."""


async def _ios_restore_and_cleanup(
    lockdown: Any, args: argparse.Namespace, summary: dict[str, Any], failures: list[str],
    switched: bool, original_identity: dict[str, Any] | None, cleanup: bool,
) -> None:
    """Restore the original vault, then (optionally) remove the scratch vault.

    Ordering is deliberate: the scratch vault can only be deleted after
    Obsidian has verifiably left it. Any incomplete step is reported in the
    summary instead of silently swallowed.
    """
    from . import ios, provision as prov

    if not switched:
        summary["restore"] = {"attempted": False, "reason": "never left the original vault"}
    elif args.keep_vault:
        summary["restore"] = {"attempted": False, "reason": "--keep-vault"}
    else:
        original_path = (original_identity or {}).get("selectedVaultPath")
        original_name = (original_identity or {}).get("vaultName")
        try:
            await _ios_switch_vault(lockdown, args.bundle, original_path)
            restored = await _with_vault_session(
                lockdown, args.bundle, original_name,
                lambda _session, identity: _async_value(identity),
            )
            summary["restore"] = {"attempted": True, "ok": True, "vaultIdentity": restored}
        except (SystemExit, TimeoutError, RuntimeError, OSError, ConnectionError) as exc:
            summary["restore"] = {"attempted": True, "ok": False, "error": str(exc)}
            failures.append("original vault was not restored")
            return  # never delete the scratch vault while Obsidian may be in it

    if cleanup:
        vault_name = summary["vault"]["name"]
        prov.guard_remove_vault(vault_name)
        if switched and not summary.get("restore", {}).get("ok"):
            # Never delete a vault Obsidian may still have open.
            summary["cleanup"] = {"attempted": False, "reason": "restore did not complete"}
            return
        vault_path = f"{prov.IOS_DOCUMENTS_ROOT}/{vault_name}"
        afc = await ios.afc_open(lockdown, args.bundle)
        try:
            undeleted = await afc.rm(vault_path, force=True) if await afc.exists(vault_path) else []
            ok = not undeleted
            summary["cleanup"] = {"attempted": True, "ok": ok, "vaultPath": vault_path}
            if not ok:
                summary["cleanup"]["undeleted"] = [str(item) for item in undeleted]
                failures.append("scratch vault cleanup incomplete")
        finally:
            await afc.close()

        # Deregister the removed vault (switcher entry + trust flag).
        scratch_open_path = (summary.get("open") or {}).get("opened")
        if ok and scratch_open_path:
            async with ios.inspector_session_unlocked(lockdown, args.bundle) as (_target, session):
                summary["cleanup"]["forgot"] = await ios.ev(
                    session, prov.forget_vault_js(scratch_open_path)
                )
    elif "cleanup" not in summary:
        summary["cleanup"] = {"attempted": False, "reason": "--cleanup not requested"}


async def _async_value(value: Any) -> Any:
    return value


# ---------- Android ----------
async def _android_wait_for_vault(port: int, expected_vault: str,
                                  timeout: float = VAULT_SWITCH_TIMEOUT) -> str | None:
    """Poll (reconnecting each time) until the expected vault reports open."""
    from . import android

    deadline = time.monotonic() + timeout
    attempts: list[str] = []
    while time.monotonic() < deadline:
        try:
            name = await android.ev(port, "app?.vault?.getName?.() ?? null", timeout=10)
            if name == expected_vault:
                return name
            attempts.append(f"open vault is {name!r}")
        except (SystemExit, TimeoutError, RuntimeError, OSError, ConnectionError) as exc:
            attempts.append(str(exc).split("\n")[0])
        await asyncio.sleep(RECONNECT_POLL_SECONDS)
    raise SystemExit(
        f"Vault {expected_vault!r} did not open within {timeout:.0f}s.\n"
        f"Attempts: {attempts[-5:]}\n"
        "The app may be showing the vault chooser - check the device screen."
    )


async def cmd_verify_android(args: argparse.Namespace) -> int:
    from . import android, provision as prov

    probes = resolve_probes(args)
    files = android.resolve_plugin_files_for_provision(args)
    if not files:
        raise SystemExit("verify needs --plugin plus --repo (or --main/--manifest)")
    data_seed = Path(args.data).expanduser().read_bytes() if args.data else None
    vault_name, vault_name_source = prov.resolve_vault_name(args.vault, args.plugin)
    prov.guard_provision_vault(
        vault_name, confirm_real=args.confirm_real_vault, test_vault=args.test_vault
    )
    if args.cleanup and args.keep_vault:
        raise SystemExit("--cleanup and --keep-vault contradict each other; pick one.")

    vault_path = android.android_vault_dir(args.vault_root, vault_name)
    summary: dict[str, Any] = {
        "action": "verify",
        "platform": "android",
        "plugin": args.plugin,
        "vault": {"name": vault_name, "source": vault_name_source, "path": vault_path},
    }
    failures: list[str] = []
    switched = False
    original_path: str | None = None

    with android.cdp_forward(args.port, args.bundle) as pid:
        summary["device"] = {"pid": pid, "cdpPort": args.port}
        try:
            # 1. diagnose + remember the original vault for restore.
            summary["diagnose"] = {"runtime": await android.read_runtime_state(args.port, args.plugin)}
            original_path = await android.ev(args.port, prov.CURRENT_SELECTED_VAULT_JS)
            original_name = summary["diagnose"]["runtime"].get("vaultName")
            summary["diagnose"]["originalVault"] = {
                "vaultName": original_name, "selectedVaultPath": original_path,
            }

            # 2. provision skeleton + plugin over adb (verification best-effort).
            skeleton = prov.vault_skeleton(args.plugin, data_seed)
            existing = android.existing_vault_files(vault_path)
            to_write = prov.plan_writes(skeleton, existing)
            for entry in to_write:
                android.write_device_file(f"{vault_path}/{entry.relpath}", entry.content)
            plugin_dir = f"{vault_path}/.obsidian/plugins/{args.plugin}"
            android.push_plugin_files(plugin_dir, files)
            summary["provision"] = {
                "vaultPath": vault_path,
                "wrote": [entry.relpath for entry in to_write],
                "skipped": sorted(existing - {entry.relpath for entry in to_write}),
                "plugin": {"pluginDir": plugin_dir, "files": sorted(files)},
            }

            # 3. switch into the scratch vault (absolute path form) and wait.
            # trust_plugins pre-clears the Restricted Mode prompt; restore
            # below deliberately never sets it.
            summary["open"] = await android.ev(
                args.port, prov.open_vault_js(vault_path, trust_plugins=True)
            )
            switched = True
            await _android_wait_for_vault(args.port, vault_name)

            # 4-5. enable plugin, then probes with console capture on one socket.
            summary["enable"] = await android.enable_plugin(args.port, args.plugin)
            summary["runtime"] = await android.read_runtime_state(args.port, args.plugin)
            plugin_state = (summary["runtime"] or {}).get("plugin") or {}
            if not plugin_state.get("enabled") or not plugin_state.get("instantiated"):
                failures.append(
                    f"plugin {args.plugin!r} is not enabled+instantiated in the scratch vault"
                )

            probe_reports: list[dict[str, Any]] = []
            for ref, source in probes:
                started = time.monotonic()
                report: dict[str, Any] = {"probe": ref}
                # Pre-allocated so a probe that throws or times out still
                # surrenders the console evidence captured before the error.
                console: list[dict[str, Any]] = []
                try:
                    value, _events = await android.ev_with_console(
                        args.port, source, timeout=args.probe_timeout, events=console,
                    )
                    report["ok"] = probe_passed(value)
                    report["result"] = value
                except (RuntimeError, TimeoutError) as exc:
                    report["ok"] = False
                    report["error"] = str(exc)
                report["console"] = console
                report["durationMs"] = round((time.monotonic() - started) * 1000)
                probe_reports.append(report)
            summary["probes"] = probe_reports
            failures.extend(
                f"probe {report['probe']!r} failed"
                for report in probe_reports if not report.get("ok")
            )

            # 6. optional post-probe capture window (the iOS logs analog).
            if args.logs_seconds:
                summary["logs"] = {
                    "seconds": args.logs_seconds,
                    "console": await android.capture_console_events(args.port, args.logs_seconds),
                }
        except BaseException:
            summary["error"] = "transport or tool failure - see stderr"
            await _android_restore_and_cleanup(
                args, summary, failures, switched, original_path, cleanup=False
            )
            summarize_assertions(summary, failures)
            emit_summary(summary)
            raise

        await _android_restore_and_cleanup(
            args, summary, failures, switched, original_path, cleanup=args.cleanup
        )

    summarize_assertions(summary, failures)
    emit_summary(summary)
    return 0 if not failures else 2


async def _android_restore_and_cleanup(
    args: argparse.Namespace, summary: dict[str, Any], failures: list[str],
    switched: bool, original_path: str | None, cleanup: bool,
) -> None:
    from . import android, provision as prov

    if not switched:
        summary["restore"] = {"attempted": False, "reason": "never left the original vault"}
    elif args.keep_vault:
        summary["restore"] = {"attempted": False, "reason": "--keep-vault"}
    elif not original_path:
        summary["restore"] = {"attempted": False, "reason": "no original vault path recorded"}
        failures.append("original vault was not restored")
        return
    else:
        try:
            await android.ev(args.port, prov.open_vault_js(original_path))
            restored_name = await _android_wait_for_vault(
                args.port, original_path.rstrip("/").rsplit("/", 1)[-1]
            )
            summary["restore"] = {"attempted": True, "ok": True, "vaultName": restored_name}
        except (SystemExit, TimeoutError, RuntimeError, OSError, ConnectionError) as exc:
            summary["restore"] = {"attempted": True, "ok": False, "error": str(exc)}
            failures.append("original vault was not restored")
            return

    if cleanup:
        vault_name = summary["vault"]["name"]
        prov.guard_remove_vault(vault_name)
        if switched and not summary.get("restore", {}).get("ok"):
            # Never delete a vault Obsidian may still have open.
            summary["cleanup"] = {"attempted": False, "reason": "restore did not complete"}
            return
        vault_path = summary["vault"]["path"]
        android.run_adb(["shell", "rm", "-rf", vault_path])
        summary["cleanup"] = {"attempted": True, "ok": True, "vaultPath": vault_path}
        # Deregister the removed vault (switcher entry + trust flag).
        summary["cleanup"]["forgot"] = await android.ev(
            args.port, prov.forget_vault_js(vault_path)
        )
    elif "cleanup" not in summary:
        summary["cleanup"] = {"attempted": False, "reason": "--cleanup not requested"}
