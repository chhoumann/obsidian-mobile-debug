#!/usr/bin/env python3
"""Validate the PodNotes streaming download against vault-watcher plugins.

Drives ONE download of PodNotes' current episode on the Android emulator over CDP
and proves (or disproves) the temp-then-move fix. It instruments Obsidian's vault
event bus, fires the download, and reports:

  - crashed            renderer/app OOM (CDP eval goes unreachable mid-download)
  - finalBytes         size of the downloaded media file, + bytePerfect vs expected
  - indexResolvable    getAbstractFileByPath(final) is a TFile (so playback works)
  - partialsLeft       any leftover ".<name>.<tok>.podnotes-partial" temp files
  - events             create/modify counts on the FINAL media path vs the temp

Run it across the matrix to show the modify-storm collapse and the crash vanish:
  old build  + Waypoint off  -> completes, storm of modify events on the .mp3
  old build  + Waypoint on   -> CRASH
  new build  + Waypoint off  -> completes, 1 create + 0 modify on the .mp3
  new build  + Waypoint on   -> completes (the fix)

  uv run --no-project --with websockets python android_waypoint_validate.py \
     --label new+wp-on --expected-bytes 48997159 [--folder PodNotes] [--max 150]

The episode is whatever PodNotes currently has loaded; set it beforehand (e.g. via
podnotes_ios.py / android_cdp.py) so re-runs hit the same file.
"""
import argparse
import asyncio
import json
import time
import urllib.request

import websockets

CDP = "http://localhost:9333"

# Recursively list every file under `dir`, returning full vault-relative paths.
WALK = (
    "async function walk(dir){const a=app.vault.adapter;let out=[];"
    "let l;try{l=await a.list(dir);}catch(e){return out;}"
    "for(const f of l.files)out.push(f);"
    "for(const d of l.folders)out=out.concat(await walk(d));return out;}"
)


def page_ws():
    data = json.load(urllib.request.urlopen(CDP + "/json", timeout=10))
    return next(p["webSocketDebuggerUrl"] for p in data if p.get("type") == "page")


def is_partial(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith(".") and name.endswith(".podnotes-partial")


def is_media(path: str) -> bool:
    return path.rsplit(".", 1)[-1].lower() in {"mp3", "m4a", "webm", "mp4", "ogv", "ogg"}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="cell label, e.g. new+wp-on")
    ap.add_argument("--folder", default="PodNotes", help="download root to scan/reset")
    ap.add_argument("--expected-bytes", type=int, default=0)
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--no-reset", action="store_true", help="keep existing downloads")
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
                    raise RuntimeError(str(res["exceptionDetails"])[:300])
                return res.get("result", {}).get("value")

    waypoint = await ev("!!app.plugins.plugins.waypoint")
    podnotes_ver = await ev("app.plugins.manifests?.podnotes?.version||'?'")
    print(f"[{args.label}] PodNotes {podnotes_ver}, Waypoint loaded={waypoint}")

    if not args.no_reset:
        reset = (
            "(async()=>{" + WALK +
            f"const a=app.vault.adapter;const fs=await walk({json.dumps(args.folder)});"
            "let n=0;for(const f of fs){try{await a.remove(f);n++;}catch(e){}}"
            "try{const p=app.plugins.plugins.podnotes;p.settings.downloadedEpisodes={};"
            "await p.saveSettings();}catch(e){}return n})()"
        )
        print(f"[{args.label}] reset: removed {await ev(reset)} files")

    # Fresh vault-event probe. Counts create/modify/rename/delete per path. The temp
    # is a dotfile (never indexed) so it fires zero events; the final file appears via
    # one rename -> exactly one create. The old in-place write fires create + a modify
    # per appended chunk on the final .mp3 itself.
    probe = (
        "(()=>{if(window.__wp&&window.__wp.refs){for(const r of window.__wp.refs)"
        "app.vault.offref(r);}const c={create:{},modify:{},rename:{},delete:{}};"
        "const b=(t,p)=>{c[t][p]=(c[t][p]||0)+1;};const refs=["
        "app.vault.on('create',f=>b('create',f.path)),"
        "app.vault.on('modify',f=>b('modify',f.path)),"
        "app.vault.on('rename',f=>b('rename',f.path)),"
        "app.vault.on('delete',f=>b('delete',f.path))];"
        "window.__wp={counts:c,refs};return 'ok'})()"
    )
    await ev(probe)

    await ev("(()=>{app.commands.executeCommandById('podnotes:download-playing-episode');return 1})()",
             await_promise=False)
    t0 = time.monotonic()
    peak = 0

    poll = (
        "(async()=>{" + WALK +
        f"const fs=await walk({json.dumps(args.folder)});const a=app.vault.adapter;"
        "let big=0,bigP='';const parts=[];for(const f of fs){"
        "const nm=f.replace(/^.*\\//,'');"
        "if(nm.startsWith('.')&&nm.endsWith('.podnotes-partial')){parts.push(f);continue;}"
        "let s=0;try{s=(await a.stat(f)).size;}catch(e){}if(s>big){big=s;bigP=f;}}"
        "const m=performance.memory||{};"
        "const n=[...document.querySelectorAll('.notice')].map(x=>x.innerText)"
        ".filter(x=>/Download|MB|%|Success|fail/i.test(x));"
        "return JSON.stringify({used:Math.round((m.usedJSHeapSize||0)/1048576),"
        "big,bigP,parts,notice:(n[n.length-1]||'').replace(/\\s+/g,' ')})})()"
    )

    result = {"label": args.label, "waypointLoaded": waypoint, "crashed": False}
    while time.monotonic() - t0 < args.max:
        await asyncio.sleep(2)
        try:
            info = json.loads(await asyncio.wait_for(ev(poll), timeout=15))
        except Exception as e:  # noqa: BLE001 - any failure here == app went unreachable
            result.update(crashed=True, detail=type(e).__name__,
                          seconds=round(time.monotonic() - t0, 1))
            break
        peak = max(peak, info["used"])
        note = info.get("notice", "")
        secs = round(time.monotonic() - t0, 1)
        print(f"  +{secs:5.1f}s heap={info['used']:4d}MB disk={info['big']/1e6:6.1f}MB "
              f"parts={len(info['parts'])} notice={note[-60:]!r}")
        if "Success" in note:
            result.update(seconds=secs, finalBytes=info["big"], finalPath=info["bigP"],
                          partialsLeft=info["parts"], peakHeapMB=peak)
            break
        if "fail" in note.lower():
            result.update(failed=True, notice=note, seconds=secs, peakHeapMB=peak)
            break

    if not result.get("crashed") and "finalBytes" in result:
        # Settle, then read final state + the event counts the probe accumulated.
        await asyncio.sleep(2)
        final = await ev(
            "(()=>{const c=(window.__wp&&window.__wp.counts)||{};"
            f"const fp={json.dumps(result['finalPath'])};"
            "const f=app.vault.getAbstractFileByPath(fp);"
            "const sum=o=>Object.entries(o||{});"
            "const onFinal=t=>(c[t]||{})[fp]||0;"
            "const onParts=t=>sum(c[t]).filter(([p])=>{const n=p.replace(/^.*\\//,'');"
            "return n.startsWith('.')&&n.endsWith('.podnotes-partial');})"
            ".reduce((a,[,v])=>a+v,0);"
            "return JSON.stringify({indexResolvable:!!(f&&f.extension),"
            "createOnFinal:onFinal('create'),modifyOnFinal:onFinal('modify'),"
            "renameOnFinal:onFinal('rename'),createOnParts:onParts('create'),"
            "modifyOnParts:onParts('modify'),allCounts:c})})()"
        )
        result["events"] = json.loads(final)
        if args.expected_bytes:
            result["expectedBytes"] = args.expected_bytes
            result["bytePerfect"] = result["finalBytes"] == args.expected_bytes

    # Best-effort: detach the probe so a later run starts clean (skip if crashed).
    if not result.get("crashed"):
        try:
            await ev("(()=>{if(window.__wp&&window.__wp.refs){for(const r of window.__wp.refs)"
                     "app.vault.offref(r);window.__wp=null;}return 1})()")
        except Exception:  # noqa: BLE001
            pass

    print("RESULT " + json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
