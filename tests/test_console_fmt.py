"""Console argument rendering: every console arg survives, in order (issue #7)."""
import json

from obsidian_mobile_debug import console_fmt
from obsidian_mobile_debug.ios import install_console_capture

RECEIVED_AT = "2026-07-13T12:00:00.000+00:00"


def event_for(message):
    return console_fmt.format_console_event(message, RECEIVED_AT)


def test_multiple_string_args_all_preserved_in_order():
    message = {
        "level": "info",
        "source": "console-api",
        "text": "[tag]",
        "parameters": [
            {"type": "string", "value": "[tag]"},
            {"type": "string", "value": '{"result":"pass","n":2}'},
        ],
    }
    event = event_for(message)
    assert event["args"] == ["[tag]", '{"result":"pass","n":2}']
    assert event["text"] == '[tag] {"result":"pass","n":2}'
    assert event["level"] == "info"
    assert event["receivedAt"] == RECEIVED_AT


def test_json_string_arg_stays_raw_not_double_encoded():
    message = {"level": "log", "text": "x", "parameters": [{"type": "string", "value": '{"a":1}'}]}
    assert event_for(message)["args"] == ['{"a":1}']


def test_primitives_null_and_undefined_stay_distinct():
    message = {
        "level": "log",
        "text": "1",
        "parameters": [
            {"type": "number", "value": 1},
            {"type": "boolean", "value": False},
            {"type": "object", "subtype": "null", "value": None},
            {"type": "undefined"},
        ],
    }
    event = event_for(message)
    assert event["args"] == [1, False, None, {"type": "undefined"}]
    assert event["text"] == "1 false null undefined"


def test_object_with_preview_renders_properties():
    message = {
        "level": "log",
        "text": "[object Object]",
        "parameters": [{
            "type": "object",
            "className": "Object",
            "description": "Object",
            "preview": {
                "properties": [
                    {"name": "obj", "type": "boolean", "value": "true"},
                    {"name": "nested", "type": "object",
                     "valuePreview": {"properties": [{"name": "k", "type": "string", "value": "v"}]}},
                ],
                "overflow": True,
            },
        }],
    }
    (arg,) = event_for(message)["args"]
    assert arg["type"] == "object"
    assert arg["className"] == "Object"
    assert arg["preview"]["obj"] == "true"
    assert arg["preview"]["nested"] == {"k": "v"}
    assert arg["preview"]["..."] == "(preview truncated)"


def test_error_renders_description():
    message = {
        "level": "error",
        "text": "Error: boom",
        "parameters": [{
            "type": "object", "subtype": "error", "className": "Error",
            "description": "Error: boom\nhandle@app.js:10",
        }],
    }
    (arg,) = event_for(message)["args"]
    assert arg == {"type": "error", "description": "Error: boom\nhandle@app.js:10"}
    assert "Error: boom" in event_for(message)["text"]


def test_function_renders_description():
    message = {"level": "log", "text": "f",
               "parameters": [{"type": "function", "description": "function f() {}"}]}
    assert event_for(message)["args"] == [{"type": "function", "description": "function f() {}"}]


def test_message_without_parameters_falls_back_to_text():
    message = {"level": "error", "source": "network",
               "text": "Failed to load resource", "url": "https://x/y.png", "line": 0}
    event = event_for(message)
    assert event["args"] == ["Failed to load resource"]
    assert event["url"] == "https://x/y.png"


def test_repeat_count_and_device_timestamp_surface():
    message = {"level": "warning", "text": "again", "repeatCount": 3, "timestamp": 123.5,
               "parameters": [{"type": "string", "value": "again"}]}
    event = event_for(message)
    assert event["repeatCount"] == 3
    assert event["deviceTimestamp"] == 123.5


def test_event_is_json_serializable():
    message = {"level": "log", "text": "x",
               "parameters": [{"type": "undefined"}, {"type": "bigint", "description": "10n"}]}
    parsed = json.loads(json.dumps(event_for(message)))
    assert parsed["args"] == [{"type": "undefined"}, {"type": "bigint", "description": "10n"}]


def test_format_console_line_shows_time_level_and_all_args():
    event = event_for({"level": "info", "text": "a",
                       "parameters": [{"type": "string", "value": "a"},
                                      {"type": "string", "value": "b"}]})
    line = console_fmt.format_console_line(event)
    assert line == "12:00:00.000+00:00 INFO    a b"


def test_format_console_line_marks_repeats():
    event = event_for({"level": "log", "text": "x", "repeatCount": 4,
                       "parameters": [{"type": "string", "value": "x"}]})
    assert console_fmt.format_console_line(event).endswith("(x4)")


class FakeSession:
    def __init__(self):
        self.response_methods = {}


def test_install_console_capture_emits_all_args_and_replays_repeats():
    session, seen = FakeSession(), []
    install_console_capture(session, seen.append)

    added = {"params": {"message": {"level": "log", "text": "a",
                                    "parameters": [{"type": "string", "value": "a"},
                                                   {"type": "string", "value": "b"}]}}}
    session.response_methods["Console.messageAdded"](added)
    session.response_methods["Console.messageRepeatCountUpdated"]({"params": {"count": 2}})

    assert len(seen) == 2
    assert seen[0]["parameters"][1]["value"] == "b"
    assert seen[1]["repeatCount"] == 2


def test_install_console_capture_repeat_before_any_message_is_noop():
    session, seen = FakeSession(), []
    install_console_capture(session, seen.append)
    session.response_methods["Console.messageRepeatCountUpdated"]({"params": {"count": 5}})
    assert seen == []
