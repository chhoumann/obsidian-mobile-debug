#!/usr/bin/env python3
"""Timed PodNotes download on the Android emulator via CDP, for the perf matrix.

Sets the current episode to the given URL, clears any prior download, fires the
download command, and measures wall-time + byte-perfection + peak JS heap +
crash. Prints one JSON line.

  uv run --no-project --with websockets python android_timed_download.py \
     --url <mp3> --title T --podcast P [--expected-bytes N]
"""
import argparse
import asyncio
import json
import time
import urllib.request

import websockets

CDP = "http://localhost:9333"


def page_ws():
    data = json.load(urllib.request.urlopen(CDP + "/json", timeout=10))
    return next(p["webSocketDebuggerUrl"] for p in data if p.get("type") == "page")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--podcast", required=True)
    ap.add_argument("--expected-bytes", type=int, default=0)
    ap.add_argument("--max", type=int, default=300)
    args = ap.parse_args()

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
                    raise RuntimeError(str(res["exceptionDetails"])[:200])
                return res.get("result", {}).get("value")

    # 1) point current episode at the target + clear any prior PodNotes download
    setup = (
        "(async()=>{const p=app.plugins.plugins.podnotes;const cur=p.api.podcast;"
        f"cur.title={json.dumps(args.title)};cur.podcastName={json.dumps(args.podcast)};"
        f"cur.streamUrl={json.dumps(args.url)};cur.url={json.dumps(args.url)};cur.mediaType='audio';"
        "const a=app.vault.adapter;try{const top=await a.list('PodNotes');for(const d of top.folders){"
        "const sub=await a.list(d);for(const f of sub.files){await a.remove(f);}}}catch(e){}"
        "try{p.settings.downloadedEpisodes={};await p.saveSettings();}catch(e){}return 'ready'})()"
    )
    print("setup:", await ev(setup))

    # 2) fire + poll
    await ev("(()=>{app.commands.executeCommandById('podnotes:download-playing-episode');return 'fired'})()", await_promise=False)
    t0 = time.monotonic()
    peak = 0
    poll = ("(async()=>{const m=performance.memory||{};let sz=0;try{const a=app.vault.adapter;const top=await a.list('PodNotes');"
            "for(const d of top.folders){const sub=await a.list(d);for(const f of sub.files){const s=await a.stat(f);if(s.size>sz)sz=s.size;}}}catch(e){}"
            "const n=[...document.querySelectorAll('.notice')].map(x=>x.innerText).filter(x=>/Download|MB|%|Success|fail/i.test(x));"
            "return JSON.stringify({used:Math.round((m.usedJSHeapSize||0)/1048576),sz,notice:(n[n.length-1]||'').replace(/\\s+/g,' ')})})()")
    result = {"crashed": False}
    while time.monotonic() - t0 < args.max:
        await asyncio.sleep(2)
        try:
            info = json.loads(await asyncio.wait_for(ev(poll), timeout=12))
        except Exception as e:  # noqa: BLE001
            result = {"crashed": True, "detail": type(e).__name__, "seconds": round(time.monotonic() - t0, 1)}
            break
        peak = max(peak, info["used"])
        note = info.get("notice", "")
        if "Success" in note:
            result = {"crashed": False, "seconds": round(time.monotonic() - t0, 1),
                      "finalBytes": info["sz"], "peakHeapMB": peak}
            break
        if "fail" in note.lower():
            result = {"crashed": False, "failed": True, "notice": note, "seconds": round(time.monotonic() - t0, 1)}
            break
    if args.expected_bytes:
        result["expectedBytes"] = args.expected_bytes
        result["bytePerfect"] = result.get("finalBytes") == args.expected_bytes
    print("RESULT " + json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
