#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol (CDP) client for the Android Obsidian WebView.

Android WebViews speak standard CDP (unlike iOS's WIP), reachable after:
  adb forward tcp:9333 localabstract:webview_devtools_remote_<pid>

Usage:
  uv run --no-project --with websockets python android_cdp.py eval '<js>'
  uv run --no-project --with websockets python android_cdp.py pages
"""
import argparse
import asyncio
import json
import sys
import urllib.request

import websockets

CDP_HTTP = "http://localhost:9333"


def discover_page_ws():
    data = json.load(urllib.request.urlopen(CDP_HTTP + "/json", timeout=10))
    pages = [p for p in data if p.get("type") == "page"]
    if not pages:
        raise SystemExit(f"No CDP 'page' target. Targets: {[p.get('type') for p in data]}")
    return pages[0]["webSocketDebuggerUrl"], pages[0].get("url", "")


async def cdp_eval(expr, await_promise=True, timeout=120.0):
    ws_url, _ = discover_page_ws()
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
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if resp.get("id") == 1:
                r = resp.get("result", {})
                if "exceptionDetails" in r:
                    exc = r["exceptionDetails"]
                    raise RuntimeError(exc.get("exception", {}).get("description") or json.dumps(exc))
                return r.get("result", {}).get("value")


async def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pages")
    pe = sub.add_parser("eval"); pe.add_argument("expr")
    pe.add_argument("--no-await", action="store_true")
    args = ap.parse_args()

    if args.cmd == "pages":
        data = json.load(urllib.request.urlopen(CDP_HTTP + "/json", timeout=10))
        for p in data:
            print(p.get("type"), "|", p.get("url", "")[:70])
        return

    val = await cdp_eval(args.expr, await_promise=not args.no_await)
    print(json.dumps(val, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
