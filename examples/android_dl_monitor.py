#!/usr/bin/env python3
"""Fire the PodNotes download on the Android emulator and watch the Chromium
renderer's JS heap + on-disk progress + liveness, to catch/measure the OOM.

  uv run --no-project --with websockets python android_dl_monitor.py
"""
import asyncio
import json
import urllib.request

import websockets

CDP = "http://localhost:9333"
FILE = "PodNotes/A Way with Words/Flash in the Pan - 22 June 2026.mp3"


def page_ws():
    data = json.load(urllib.request.urlopen(CDP + "/json", timeout=10))
    return next(p["webSocketDebuggerUrl"] for p in data if p.get("type") == "page")


async def main():
    ws = await websockets.connect(page_ws(), max_size=None, open_timeout=20)
    _id = 0

    async def ev(expr, await_promise=True, timeout=30):
        nonlocal _id
        _id += 1
        mid = _id
        await ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate", "params": {
            "expression": expr, "returnByValue": True, "awaitPromise": await_promise,
            "allowUnsafeEvalBlockedByCSP": True, "userGesture": True}}))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if r.get("id") == mid:
                res = r.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(res["exceptionDetails"].get("exception", {}).get("description", "exc"))
                return res.get("result", {}).get("value")

    poll = ("(async()=>{const m=performance.memory||{};let sz=0;try{const a=app.vault.adapter;"
            f"if(await a.exists({json.dumps(FILE)}))sz=(await a.stat({json.dumps(FILE)})).size;}}catch(e){{}}"
            "const f=[...document.querySelectorAll('.notice')].map(n=>n.innerText).filter(x=>/Download|MB|%|Success|fail/i.test(x));"
            "return JSON.stringify({usedMB:Math.round((m.usedJSHeapSize||0)/1048576),totalMB:Math.round((m.totalJSHeapSize||0)/1048576),"
            "limitMB:Math.round((m.jsHeapSizeLimit||0)/1048576),fileMB:Math.round(sz/1048576),notice:(f[f.length-1]||'').slice(0,55)})})()")

    base = await ev(poll)
    print("baseline:", base)
    print("firing download...")
    await ev("(()=>{app.commands.executeCommandById('podnotes:download-playing-episode');return 'fired'})()", await_promise=False)

    t = 0
    while t < 150:
        await asyncio.sleep(2)
        t += 2
        try:
            info = await asyncio.wait_for(ev(poll), timeout=12)
        except Exception as e:  # noqa: BLE001
            print(f"\n💥 renderer UNREACHABLE at ~{t}s ({type(e).__name__}) — WebView crashed")
            return
        print(f"+{t:>3}s {info}")
        d = json.loads(info)
        if "Success" in (d.get("notice") or "") or "fail" in (d.get("notice") or "").lower():
            print("\n(terminal notice reached)")
            return
    print("\n(no crash within window)")


if __name__ == "__main__":
    asyncio.run(main())
