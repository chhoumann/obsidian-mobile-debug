"""Pure pieces of the verify loop: probe resolution, pass rules, CLI, events."""
import argparse
import asyncio
import contextlib
import json

import pytest

from obsidian_mobile_debug import android, verify
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


@contextlib.contextmanager
def _cdp_page(frames):
    """A real local server standing in for a CDP page: /json listing plus its websocket."""
    import threading
    from http import HTTPStatus

    from websockets.sync.server import serve

    def process_request(connection, request):
        if request.path == "/json":
            url = f"ws://127.0.0.1:{connection.local_address[1]}/devtools/page/1"
            return connection.respond(
                HTTPStatus.OK, json.dumps([{"type": "page", "url": "url", "webSocketDebuggerUrl": url}])
            )

    def handler(ws):
        for frame in frames:
            ws.send(json.dumps(frame))
        for _message in ws:  # hold the socket open until the client closes it
            pass

    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield server.socket.getsockname()[1]
        server.shutdown()


def test_ev_with_console_preserves_events_on_probe_exception():
    """A probe that throws must not discard console output captured before it."""
    from obsidian_mobile_debug import android

    frames = [
        {"method": "Runtime.consoleAPICalled",
         "params": {"type": "error", "args": [{"type": "string", "value": "diag"}]}},
        {"id": 2, "result": {"exceptionDetails": {"exception": {"description": "Error: probe blew up"}}}},
    ]
    events = []

    async def run():
        with _cdp_page(frames) as port:
            await android.ev_with_console(port, "boom()", timeout=5, events=events)

    with pytest.raises(RuntimeError, match="probe blew up"):
        asyncio.run(run())
    assert len(events) == 1
    assert events[0]["args"] == ["diag"]
    assert events[0]["level"] == "error"


def test_capture_console_events_collects_until_window_closes():
    from obsidian_mobile_debug import android

    frames = [
        {"method": "Runtime.consoleAPICalled",
         "params": {"type": "log", "args": [{"type": "string", "value": "tail"}]}},
    ]

    async def run():
        with _cdp_page(frames) as port:
            return await android.capture_console_events(port, 0.3)

    assert [event["text"] for event in asyncio.run(run())] == ["tail"]


def test_ev_ignores_proxy_environment(monkeypatch):
    """CDP is an adb-forwarded localhost port; a proxy can't reach it."""
    from obsidian_mobile_debug import android

    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")

    async def run():
        with _cdp_page([{"id": 1, "result": {"result": {"value": 2}}}]) as port:
            return await android.ev(port, "1 + 1", timeout=5)

    assert asyncio.run(run()) == 2


# ---------- restore/cleanup safety against a fake phone ----------
from fake_phone import (
    ANDROID_ROOT,
    ANDROID_SCRATCH_DIR,
    IOS_SCRATCH_DIR,
    SCRATCH,
    FakePhone,
    fake_android,
    fake_ios,
    outcome,
)


def test_ios_verify_cleanup_restores_then_deletes_scratch(monkeypatch, capsys):
    phone = FakePhone("documents/notes", {"/Documents/notes"})
    fake_ios(monkeypatch, phone)
    args = parse("ios", "verify", "--plugin", "quickadd", "--cleanup")
    assert outcome(verify.cmd_verify_ios(object(), args)) == 0
    assert phone.selected == "documents/notes"
    assert phone.dirs == {"/Documents/notes"}


def test_ios_verify_cleanup_refuses_when_started_in_scratch(monkeypatch, capsys):
    phone = FakePhone(f"documents/{SCRATCH}", {"/Documents/notes", IOS_SCRATCH_DIR})
    fake_ios(monkeypatch, phone)
    args = parse("ios", "verify", "--plugin", "quickadd", "--cleanup")
    result = outcome(verify.cmd_verify_ios(object(), args))
    assert IOS_SCRATCH_DIR in phone.dirs
    assert isinstance(result, SystemExit) and "--keep-vault" in str(result)


def test_ios_cleanup_never_deletes_the_open_vault(monkeypatch):
    # Cleanup without a completed vault switch (e.g. artifact verification
    # failed) skips restore; the path check alone must stop the delete.
    phone = FakePhone(f"documents/{SCRATCH}", {IOS_SCRATCH_DIR})
    fake_ios(monkeypatch, phone)
    args = parse("ios", "verify", "--plugin", "quickadd", "--cleanup")
    summary, failures = {"vault": {"name": SCRATCH}}, []
    outcome(verify._ios_restore_and_cleanup(
        object(), args, summary, failures, False, None, cleanup=True
    ))
    assert IOS_SCRATCH_DIR in phone.dirs
    assert summary["cleanup"]["attempted"] is False
    assert failures


def test_ios_cleanup_refuses_name_match_without_recorded_path(monkeypatch):
    phone = FakePhone(None, {IOS_SCRATCH_DIR}, loaded=SCRATCH)
    fake_ios(monkeypatch, phone)
    args = parse("ios", "verify", "--plugin", "quickadd", "--cleanup")
    summary, failures = {"vault": {"name": SCRATCH}}, []
    outcome(verify._ios_restore_and_cleanup(
        object(), args, summary, failures, False, None, cleanup=True
    ))
    assert IOS_SCRATCH_DIR in phone.dirs
    assert summary["cleanup"]["attempted"] is False
    assert failures


def test_android_verify_cleanup_restores_then_deletes_scratch(monkeypatch, capsys):
    notes = f"{ANDROID_ROOT}/notes"
    phone = FakePhone(notes, {notes})
    fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    assert outcome(verify.cmd_verify_android(args)) == 0
    assert phone.selected == notes
    assert phone.dirs == {notes}


def test_android_verify_from_a_fresh_install_returns_to_no_vault(monkeypatch, capsys):
    phone = FakePhone(None, set(), all_files_access=False)
    fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    assert outcome(verify.cmd_verify_android(args)) == 0
    assert phone.vault_name is None and phone.selected is None
    assert phone.dirs == set()
    summary = json.loads(capsys.readouterr().out)
    assert summary["open"]["allFilesAccess"] == "granted"
    assert summary["restore"] == {"attempted": True, "ok": True, "vaultName": None}


def test_android_verify_ignores_a_selection_obsidian_could_not_open(monkeypatch, capsys):
    phone = FakePhone(ANDROID_SCRATCH_DIR, {ANDROID_SCRATCH_DIR}, all_files_access=False)
    fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    assert outcome(verify.cmd_verify_android(args)) == 0
    assert phone.selected is None
    assert phone.dirs == set()


def test_android_verify_cleanup_refuses_while_scratch_is_still_loading(monkeypatch, capsys):
    monkeypatch.setattr(android, "RECONNECT_POLL_SECONDS", 0)
    phone = FakePhone(ANDROID_SCRATCH_DIR, {ANDROID_SCRATCH_DIR}, starting=True)
    fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    result = outcome(verify.cmd_verify_android(args))
    assert ANDROID_SCRATCH_DIR in phone.dirs
    assert isinstance(result, SystemExit) and "--keep-vault" in str(result)


def test_android_verify_cleanup_refuses_when_started_in_scratch(monkeypatch, capsys):
    phone = FakePhone(ANDROID_SCRATCH_DIR, {ANDROID_SCRATCH_DIR})
    fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    result = outcome(verify.cmd_verify_android(args))
    assert ANDROID_SCRATCH_DIR in phone.dirs
    assert isinstance(result, SystemExit) and "--keep-vault" in str(result)


def test_android_cleanup_never_deletes_the_open_vault(monkeypatch):
    phone = FakePhone(ANDROID_SCRATCH_DIR, {ANDROID_SCRATCH_DIR})
    fake_android(monkeypatch, phone)
    args = parse("android", "verify", "--plugin", "quickadd", "--cleanup")
    summary = {"vault": {"name": SCRATCH, "path": ANDROID_SCRATCH_DIR}}
    failures = []
    outcome(verify._android_restore_and_cleanup(
        args, summary, failures, False, None, None, cleanup=True
    ))
    assert ANDROID_SCRATCH_DIR in phone.dirs
    assert summary["cleanup"]["attempted"] is False
    assert failures
