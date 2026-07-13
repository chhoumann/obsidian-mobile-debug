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
