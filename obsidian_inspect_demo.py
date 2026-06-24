#!/usr/bin/env python3
"""Connect to Obsidian's WKWebView on a USB-connected iPhone via the WebKit
Web Inspector protocol (pymobiledevice3) and evaluate JavaScript in it.

Read-only demo: it only *reads* state from the Obsidian app. Results are
JSON-serialized on the device side to avoid a null-handling quirk in
InspectorSession._parse_runtime_evaluate.
"""
import asyncio
import json

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.webinspector import WebinspectorService

OBSIDIAN_BUNDLE = "md.obsidian"

# (label, JS expression). Each is wrapped in JSON.stringify() before eval.
EXPRESSIONS = [
    ("document.title", "document.title"),
    ("obsidian api version", "window.apiVersion ?? null"),
    ("vault name", "app.vault.getName()"),
    ("# markdown files", "app.vault.getMarkdownFiles().length"),
    ("active file", "app.workspace.getActiveFile()?.path ?? null"),
    ("current theme", "app.vault.getConfig?.('theme') ?? null"),
    ("appearance (dark?)", "document.body.classList.contains('theme-dark')"),
    ("5 most-recent md files",
     "app.vault.getMarkdownFiles().sort((a,b)=>b.stat.mtime-a.stat.mtime).slice(0,5).map(f=>f.path)"),
    ("total words in active note",
     "(async()=>{const f=app.workspace.getActiveFile(); if(!f) return null; "
     "const t=await app.vault.cachedRead(f); return t.trim().split(/\\s+/).filter(Boolean).length;})()"),
]


async def evaluate(session, exp, awaitable=False):
    """Evaluate `exp` and return a Python value (via JSON round-trip)."""
    wrapped = f"JSON.stringify({exp})"
    if awaitable:
        wrapped = f"Promise.resolve({exp}).then(v=>JSON.stringify(v))"
        # awaitPromise isn't set by the lib's helper, so resolve client-side instead:
        wrapped = (
            "(()=>{let __r;"
            f"Promise.resolve({exp}).then(v=>{{__r=JSON.stringify(v);}});"
            "return new Promise(res=>{const t=setInterval(()=>{if(__r!==undefined){clearInterval(t);res(__r);}},10);});})()"
        )
    raw = await session.runtime_evaluate(wrapped, return_by_value=True)
    if raw is None or raw == "undefined":
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


async def main():
    lockdown = await create_using_usbmux()
    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()
    try:
        async with inspector:
            pages = await inspector.get_open_application_pages(timeout=3)

            target = None
            for ap in pages:
                name = (ap.application.name or "").lower()
                if ap.application.bundle == OBSIDIAN_BUNDLE or "obsidian" in name:
                    target = ap
                    break

            if target is None:
                print("No Obsidian page found. Inspectable pages:")
                for ap in pages:
                    print("  ", ap)
                return

            print(f"Connected: {target}")
            print("-" * 64)

            session = await inspector.inspector_session(target.application, target.page)
            await session.runtime_enable()

            for label, exp in EXPRESSIONS[:-1]:
                try:
                    res = await evaluate(session, exp)
                except Exception as e:  # noqa: BLE001
                    res = f"<error: {type(e).__name__}: {e}>"
                print(f"{label:>26} : {res}")

            # the async one (word count) needs promise resolution
            label, exp = EXPRESSIONS[-1]
            try:
                res = await evaluate(session, exp, awaitable=True)
            except Exception as e:  # noqa: BLE001
                res = f"<error: {type(e).__name__}: {e}>"
            print(f"{label:>26} : {res}")
    finally:
        await inspector.close()


if __name__ == "__main__":
    asyncio.run(main())
