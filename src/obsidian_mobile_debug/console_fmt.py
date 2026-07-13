"""Rendering for WebKit ``Console.messageAdded`` events (iOS Web Inspector).

pymobiledevice3's bundled console logger prints only ``message.text``, which
WebKit populates from the *first* console argument - everything after it lives
in ``message.parameters`` as RemoteObjects and was silently dropped. This
module renders every argument, in order, in two shapes:

- a structured event (``format_console_event``) whose ``args`` array keeps
  argument boundaries for ``--json`` consumers, and
- a human-readable line (``format_console_line``).

Everything here is pure: it takes protocol dicts and returns values, so the
argument-rendering rules are unit-testable without a device.

RemoteObject rendering rules (stable by design, tests pin them):

- a primitive with a ``value`` key renders as that value (strings stay raw);
- JS ``null`` renders as JSON ``null``;
- JS ``undefined`` renders as ``{"type": "undefined"}`` so it stays distinct
  from ``null``;
- an error renders as ``{"type": "error", "description": ...}`` (description
  carries the message and, when WebKit includes it, the stack);
- any other object renders as ``{"type": "object", "className": ...,
  "preview": {...}}`` from the WebKit-generated preview when present, else its
  ``description``;
- a function renders as ``{"type": "function", "description": ...}``.
"""
from __future__ import annotations

import json
from typing import Any

UNDEFINED = {"type": "undefined"}


def _render_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """Flatten a WebKit ObjectPreview into {name: printable-value}.

    Preview property values are already strings on the wire; nested previews
    (``valuePreview``) recurse. An ``overflow`` preview gets a ``...`` marker so
    truncation is visible instead of silent.
    """
    rendered: dict[str, Any] = {}
    for prop in preview.get("properties") or []:
        name = prop.get("name", "?")
        if "valuePreview" in prop:
            rendered[name] = _render_preview(prop["valuePreview"])
        else:
            rendered[name] = prop.get("value")
    if preview.get("overflow"):
        rendered["..."] = "(preview truncated)"
    return rendered


def render_remote_object(obj: Any) -> Any:
    """JSON-safe representation of one WebKit RemoteObject console argument."""
    if not isinstance(obj, dict):
        return obj

    obj_type = obj.get("type")
    subtype = obj.get("subtype")

    if obj_type == "undefined":
        return dict(UNDEFINED)
    if subtype == "null":
        return None
    if "value" in obj:
        return obj["value"]
    if subtype == "error":
        rendered: dict[str, Any] = {"type": "error", "description": obj.get("description")}
        if "preview" in obj:
            rendered["preview"] = _render_preview(obj["preview"])
        return rendered
    if obj_type == "function":
        return {"type": "function", "description": obj.get("description")}
    if obj_type == "object" or "preview" in obj:
        rendered = {"type": "object", "className": obj.get("className")}
        if "preview" in obj:
            rendered["preview"] = _render_preview(obj["preview"])
        elif obj.get("description") is not None:
            rendered["description"] = obj.get("description")
        return rendered
    # bigint / symbol / anything WebKit describes but does not value-encode.
    return {"type": obj_type, "description": obj.get("description")}


def render_arg_text(rendered: Any) -> str:
    """One argument as console-like text: strings raw, everything else compact JSON."""
    if isinstance(rendered, str):
        return rendered
    if rendered == UNDEFINED:
        return "undefined"
    if isinstance(rendered, dict) and rendered.get("type") == "error":
        return str(rendered.get("description") or "Error")
    return json.dumps(rendered, ensure_ascii=False)


def format_console_event(message: dict[str, Any], received_at: str) -> dict[str, Any]:
    """Structured event for one Console.messageAdded ``message`` payload.

    ``parameters`` (when present) carries *all* console arguments including the
    first, so it is authoritative; ``text`` is only the fallback for messages
    WebKit reports without parameters (e.g. network errors).
    """
    parameters = message.get("parameters")
    if parameters:
        args = [render_remote_object(parameter) for parameter in parameters]
    else:
        args = [message.get("text")]

    event: dict[str, Any] = {
        "event": "console",
        "level": message.get("level", "log"),
        "source": message.get("source"),
        "receivedAt": received_at,
        "args": args,
        "text": " ".join(render_arg_text(arg) for arg in args),
    }
    if message.get("url"):
        event["url"] = message["url"]
        if message.get("line"):
            event["line"] = message["line"]
    if message.get("repeatCount", 1) > 1:
        event["repeatCount"] = message["repeatCount"]
    if message.get("timestamp") is not None:
        event["deviceTimestamp"] = message["timestamp"]
    return event


def format_console_line(event: dict[str, Any]) -> str:
    """Human-readable line: time, level, then every argument in order."""
    time_part = event["receivedAt"].split("T")[-1]
    line = f"{time_part} {event['level'].upper():7s} {event['text']}"
    if event.get("repeatCount"):
        line += f"  (x{event['repeatCount']})"
    return line
