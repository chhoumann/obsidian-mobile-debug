"""Pure pieces of the verify loop: probe resolution, pass rules, CLI, events."""
import argparse

import pytest

from obsidian_mobile_debug import verify
from obsidian_mobile_debug.android import format_cdp_console_event
from obsidian_mobile_debug.cli import build_parser


def parse(*argv):
    return build_parser().parse_args(argv)


def test_probe_passed_rules():
    assert verify.probe_passed({"ok": True})
    assert verify.probe_passed({"anything": 1})
    assert verify.probe_passed("string result")
    assert verify.probe_passed(None)
    assert not verify.probe_passed({"ok": False})
    assert not verify.probe_passed({"ok": False, "reason": "x"})


def test_resolve_probes_defaults_to_core_smoke():
    args = argparse.Namespace(probe=None)
    resolved = verify.resolve_probes(args)
    assert [ref for ref, _source in resolved] == ["core_smoke"]
    assert all(source.strip() for _ref, source in resolved)


def test_resolve_probes_keeps_order_and_paths(tmp_path):
    probe = tmp_path / "custom.js"
    probe.write_text("({ok: true})", encoding="utf-8")
    args = argparse.Namespace(probe=["core_smoke", str(probe)])
    resolved = verify.resolve_probes(args)
    assert [ref for ref, _source in resolved] == ["core_smoke", str(probe)]
    assert resolved[1][1] == "({ok: true})"


def test_resolve_probes_unknown_probe_is_tool_error():
    with pytest.raises(SystemExit):
        verify.resolve_probes(argparse.Namespace(probe=["does_not_exist"]))


def test_summarize_assertions():
    summary = {}
    verify.summarize_assertions(summary, [])
    assert summary["assertions"] == {"passed": True, "failures": []}
    verify.summarize_assertions(summary, ["probe 'x' failed"])
    assert summary["assertions"]["passed"] is False


def test_cli_ios_verify_args():
    args = parse("ios", "verify", "--plugin", "quickadd", "--repo", "/tmp/qa",
                 "--probe", "core_smoke", "--probe", "./p.js", "--logs-seconds", "10")
    assert args.cmd == "verify"
    assert args.plugin == "quickadd"
    assert args.probe == ["core_smoke", "./p.js"]
    assert args.logs_seconds == 10
    assert args.vault is None
    assert args.keep_vault is False
    assert args.cleanup is False


def test_cli_android_verify_has_port_and_root():
    args = parse("android", "verify", "--plugin", "quickadd", "--repo", "/tmp/qa")
    assert args.port == 9333
    assert args.vault_root == "/storage/emulated/0/Documents"


def test_cli_verify_requires_plugin():
    with pytest.raises(SystemExit):
        parse("ios", "verify")


def test_format_cdp_console_event_maps_types_and_args():
    params = {
        "type": "warning",
        "timestamp": 1783954632855.5,
        "args": [
            {"type": "string", "value": "[tag]"},
            {"type": "object", "subtype": "null", "value": None},
            {"type": "undefined"},
        ],
    }
    event = format_cdp_console_event(params, "2026-07-13T12:00:00.000+00:00")
    assert event["level"] == "warning"
    assert event["args"] == ["[tag]", None, {"type": "undefined"}]
    assert event["text"] == "[tag] null undefined"
    assert event["deviceTimestamp"] == 1783954632855.5


def test_format_cdp_console_event_unknown_type_falls_back_to_log():
    event = format_cdp_console_event({"type": "table", "args": []}, "t")
    assert event["level"] == "log"


class _FakeWS:
    """Minimal websocket stand-in feeding scripted CDP frames."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        import asyncio
        if not self.frames:
            await asyncio.sleep(3600)
        return self.frames.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _connectable(ws):
    class _Conn:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return ws

        async def __aexit__(self, *exc):
            return False

    return lambda *a, **k: _Conn()


def test_ev_with_console_preserves_events_on_probe_exception(monkeypatch):
    """A probe that throws must not discard console output captured before it."""
    import asyncio
    import json as _json
    import sys
    import types

    from obsidian_mobile_debug import android

    frames = [
        _json.dumps({"method": "Runtime.consoleAPICalled",
                     "params": {"type": "error", "args": [{"type": "string", "value": "diag"}]}}),
        _json.dumps({"id": 2, "result": {"exceptionDetails": {
            "exception": {"description": "Error: probe blew up"}}}}),
    ]
    ws = _FakeWS(frames)
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=_connectable(ws)))
    monkeypatch.setattr(android, "discover_page_ws", lambda port: ("ws://fake", "url"))

    events = []
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="probe blew up"):
        asyncio.run(android.ev_with_console(9333, "boom()", timeout=5, events=events))
    assert len(events) == 1
    assert events[0]["args"] == ["diag"]
    assert events[0]["level"] == "error"


def test_capture_console_events_collects_until_window_closes(monkeypatch):
    import asyncio
    import json as _json
    import sys
    import types

    from obsidian_mobile_debug import android

    frames = [
        _json.dumps({"method": "Runtime.consoleAPICalled",
                     "params": {"type": "log", "args": [{"type": "string", "value": "tail"}]}}),
    ]
    ws = _FakeWS(frames)
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=_connectable(ws)))
    monkeypatch.setattr(android, "discover_page_ws", lambda port: ("ws://fake", "url"))

    events = asyncio.run(android.capture_console_events(9333, 0.3))
    assert [event["text"] for event in events] == ["tail"]


# ---------- restore/cleanup safety against a fake phone ----------
# The fake models the two facts cleanup safety depends on: which vault path
# Obsidian has selected (localStorage) and which vault dirs exist on disk.
# Everything else in the verify flow runs for real.
import contextlib
import json
import re

from obsidian_mobile_debug import provision as prov

SCRATCH = "quickadd-omd-scratch"
IOS_SCRATCH_DIR = f"/Documents/{SCRATCH}"
ANDROID_ROOT = "/storage/emulated/0/Documents"
ANDROID_SCRATCH_DIR = f"{ANDROID_ROOT}/{SCRATCH}"
ANDROID_NAME_JS = "app?.vault?.getName?.() ?? null"


class _FakePhone:
    def __init__(self, selected, dirs):
        self.selected = selected
        self.dirs = set(dirs)

    @property
    def vault_name(self):
        return self.selected.rstrip("/").rsplit("/", 1)[-1] if self.selected else None

    def ev(self, expr):
        if expr == prov.CURRENT_SELECTED_VAULT_JS:
            return self.selected
        if expr == ANDROID_NAME_JS:
            return self.vault_name
        if "location.reload" in expr:  # open_vault_js: select the path and reload
            self.selected = json.loads(re.search(r'const p = ("[^"]*");', expr).group(1))
            return {"opened": self.selected}
        return {"ok": True}

    def runtime(self):
        return {"vaultName": self.vault_name, "plugin": {"enabled": True, "instantiated": True}}


def _outcome(coro):
    import asyncio

    try:
        return asyncio.run(coro)
    except SystemExit as exc:
        return exc


def _fake_ios(monkeypatch, phone):
    from obsidian_mobile_debug import ios, lock

    class _Session:
        async def console_enable(self):
            pass

    @contextlib.asynccontextmanager
    async def session_cm(_lockdown, _bundle):
        yield "target", _Session()

    class _AFC:
        async def exists(self, path):
            return path in phone.dirs

        async def rm(self, path, force=False):
            phone.dirs.discard(path)
            return []

        async def close(self):
            pass

    async def afc_open(_lockdown, _bundle):
        return _AFC()

    async def provision_scratch_vault(_afc, name, *_rest):
        phone.dirs.add(f"/Documents/{name}")
        return {"plugin": {"pushed": {"main.js": {"ok": True}}}}

    async def ev(_session, expr, timeout=30.0):
        return phone.ev(expr)

    async def read_vault_identity(_session):
        return prov.vault_identity(phone.vault_name, phone.selected)

    async def read_runtime_state(_session, _plugin):
        return phone.runtime()

    async def enable_plugin(_session, _plugin):
        return {}

    monkeypatch.setattr(lock, "inspector_lock", lambda *_a: contextlib.nullcontext())
    monkeypatch.setattr(ios, "inspector_session_unlocked", session_cm)
    monkeypatch.setattr(ios, "afc_open", afc_open)
    monkeypatch.setattr(ios, "provision_scratch_vault", provision_scratch_vault)
    monkeypatch.setattr(ios, "resolve_plugin_files", lambda _args: {})
    monkeypatch.setattr(ios, "ev", ev)
    monkeypatch.setattr(ios, "read_vault_identity", read_vault_identity)
    monkeypatch.setattr(ios, "read_runtime_state", read_runtime_state)
    monkeypatch.setattr(ios, "enable_plugin", enable_plugin)
    monkeypatch.setattr(ios, "install_console_capture", lambda *_a: None)


def _fake_android(monkeypatch, phone):
    from obsidian_mobile_debug import android

    @contextlib.contextmanager
    def cdp_forward(_port, _package):
        yield 4242

    async def ev(_port, expr, *, timeout=120.0, await_promise=True):
        return phone.ev(expr)

    async def ev_with_console(_port, expr, *, timeout=120.0, events=None):
        return phone.ev(expr), []

    async def read_runtime_state(_port, _plugin):
        return phone.runtime()

    async def enable_plugin(_port, _plugin):
        return {}

    def run_adb(args, *, check=True):
        if args[:3] == ["shell", "rm", "-rf"]:
            phone.dirs.discard(args[3])

    def write_device_file(path, _content):
        phone.dirs.add(path.split("/.obsidian/")[0])

    monkeypatch.setattr(android, "cdp_forward", cdp_forward)
    monkeypatch.setattr(android, "ev", ev)
    monkeypatch.setattr(android, "ev_with_console", ev_with_console)
    monkeypatch.setattr(android, "read_runtime_state", read_runtime_state)
    monkeypatch.setattr(android, "enable_plugin", enable_plugin)
    monkeypatch.setattr(android, "resolve_plugin_files_for_provision", lambda _a: {"main.js": "m"})
    monkeypatch.setattr(android, "existing_vault_files", lambda _path: set())
    monkeypatch.setattr(android, "write_device_file", write_device_file)
    monkeypatch.setattr(android, "push_plugin_files", lambda *_a: None)
    monkeypatch.setattr(android, "run_adb", run_adb)


def test_ios_verify_cleanup_restores_then_deletes_scratch(monkeypatch, capsys):
    phone = _FakePhone("documents/notes", {"/Documents/notes"})
    _fake_ios(monkeypatch, phone)
    args = parse("ios", "verify", "--plugin", "quickadd", "--cleanup")
    assert _outcome(verify.cmd_verify_ios(object(), args)) == 0
    assert phone.selected == "documents/notes"
    assert phone.dirs == {"/Documents/notes"}


def test_ios_verify_cleanup_refuses_when_started_in_scratch(monkeypatch, capsys):
    phone = _FakePhone(f"documents/{SCRATCH}", {"/Documents/notes", IOS_SCRATCH_DIR})
    _fake_ios(monkeypatch, phone)
    args = parse("ios", "verify", "--plugin", "quickadd", "--cleanup")
    result = _outcome(verify.cmd_verify_ios(object(), args))
    assert IOS_SCRATCH_DIR in phone.dirs
    assert isinstance(result, SystemExit) and "--keep-vault" in str(result)


def test_ios_cleanup_never_deletes_the_open_vault(monkeypatch):
    # Cleanup without a completed vault switch (e.g. artifact verification
    # failed) skips restore; the path check alone must stop the delete.
    phone = _FakePhone(f"documents/{SCRATCH}", {IOS_SCRATCH_DIR})
    _fake_ios(monkeypatch, phone)
    args = parse("ios", "verify", "--plugin", "quickadd", "--cleanup")
    summary, failures = {"vault": {"name": SCRATCH}}, []
    _outcome(verify._ios_restore_and_cleanup(
        object(), args, summary, failures, False, None, cleanup=True
    ))
    assert IOS_SCRATCH_DIR in phone.dirs
    assert summary["cleanup"]["attempted"] is False
    assert failures


def test_android_verify_cleanup_restores_then_deletes_scratch(monkeypatch, capsys):
    notes = f"{ANDROID_ROOT}/notes"
    phone = _FakePhone(notes, {notes})
    _fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    assert _outcome(verify.cmd_verify_android(args)) == 0
    assert phone.selected == notes
    assert phone.dirs == {notes}


def test_android_verify_cleanup_refuses_when_started_in_scratch(monkeypatch, capsys):
    phone = _FakePhone(ANDROID_SCRATCH_DIR, {ANDROID_SCRATCH_DIR})
    _fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    result = _outcome(verify.cmd_verify_android(args))
    assert ANDROID_SCRATCH_DIR in phone.dirs
    assert isinstance(result, SystemExit) and "--keep-vault" in str(result)


def test_android_cleanup_never_deletes_the_open_vault(monkeypatch):
    phone = _FakePhone(ANDROID_SCRATCH_DIR, {ANDROID_SCRATCH_DIR})
    _fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    summary = {"vault": {"name": SCRATCH, "path": ANDROID_SCRATCH_DIR}}
    failures = []
    _outcome(verify._android_restore_and_cleanup(
        args, summary, failures, False, None, cleanup=True
    ))
    assert ANDROID_SCRATCH_DIR in phone.dirs
    assert summary["cleanup"]["attempted"] is False
    assert failures
