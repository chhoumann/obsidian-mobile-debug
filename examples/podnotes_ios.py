#!/usr/bin/env python3
"""Drive Obsidian on a USB-connected iPhone via the WebKit Web Inspector.

Solves the sync gap: pushes a locally-built Obsidian plugin (main.js /
manifest.json / styles.css) straight into the phone's vault over USB, then
enables/reloads it — no App Store, no file sync, no cable copy dance.

Subcommands:
  diagnose                 Report the plugin's install/enable state on the phone.
  eval "<js>"              Evaluate a JS expression (async-aware) -> JSON.
  deploy                   Build outputs -> phone, then enable/reload + report errors.
  reload                   disable+enable the plugin on the phone.
  logs [--seconds N]       Stream console + uncaught errors from the webview.

Run with:
  uv run --no-project --with pymobiledevice3 python podnotes_ios.py <cmd>
  # or: ~/.local/share/uv/tools/pymobiledevice3/bin/python podnotes_ios.py <cmd>
"""
import argparse
import asyncio
import json
import logging
import os

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.webinspector import WebinspectorService

BUNDLE = "md.obsidian"
PLUGIN_ID = "podnotes"
REPO = "/Users/christian/Developer/PodNotes"
# name-on-phone -> local source path. styles.css is added only if it exists.
LOCAL_FILES = {
    "main.js": f"{REPO}/build/main.js",
    "manifest.json": f"{REPO}/manifest.json",
}
STYLES_SRC = f"{REPO}/styles.css"
TOKEN = "__pmd_r"


# ---------- connection ----------
async def open_session(inspector):
    pages = await inspector.get_open_application_pages(timeout=3)
    target = next(
        (ap for ap in pages
         if ap.application.bundle == BUNDLE or "obsidian" in (ap.application.name or "").lower()),
        None,
    )
    if target is None:
        raise SystemExit(
            "No Obsidian page found. Unlock the phone, open Obsidian, ensure "
            "Settings > Apps > Safari > Advanced > Web Inspector is ON.\n"
            f"Pages seen: {[str(p) for p in pages]}"
        )
    session = await inspector.inspector_session(target.application, target.page)
    await session.runtime_enable()
    return target, session


# ---------- robust async-aware eval (stash + poll; dodges null/promise quirks) ----------
async def ev(session, expr, timeout=30.0):
    kickoff = (
        f"(()=>{{window.{TOKEN}=undefined;"
        f"(async()=>{{try{{window.{TOKEN}={{ok:JSON.stringify(await ({expr}))}}}}"
        f"catch(e){{window.{TOKEN}={{err:String((e&&e.stack)||e)}}}}}})();return 0}})()"
    )
    await session.runtime_evaluate(kickoff, return_by_value=True)
    waited = 0.0
    poll = f"JSON.stringify(window.{TOKEN}===undefined?null:window.{TOKEN})"
    while waited < timeout:
        r = await session.runtime_evaluate(poll, return_by_value=True)
        if r and r != "null":
            obj = json.loads(r)
            if "err" in obj:
                raise RuntimeError(obj["err"])
            v = obj.get("ok")
            if v is None:
                return None
            try:
                return json.loads(v)
            except (TypeError, json.JSONDecodeError):
                return v
        await asyncio.sleep(0.1)
        waited += 0.1
    raise TimeoutError(f"eval timed out: {expr[:80]}")


def js(s):
    return json.dumps(s)


async def config_dir(session):
    return await ev(session, "app.vault.configDir")


# ---------- file transfer over AFC (house_arrest Documents container) ----------
# AFC root with documents_only maps the app's Documents dir under /Documents.
async def afc_open(lockdown):
    return await HouseArrestService.create(lockdown, BUNDLE, documents_only=True)


async def afc_find_vault(afc, prefer):
    for name in await afc.listdir("/Documents"):
        if name in (".", ".."):
            continue
        try:
            if ".obsidian" in await afc.listdir(f"/Documents/{name}"):
                if prefer is None or name == prefer:
                    return f"/Documents/{name}"
        except Exception:  # noqa: BLE001
            continue
    raise SystemExit("No Obsidian vault (.obsidian) found under the app's Documents.")


async def afc_put(afc, remote_path, data: bytes):
    await afc.set_file_contents(remote_path, data)
    st = await afc.stat(remote_path)
    return int(st.get("st_size", st.get("size", -1)))


# ---------- subcommands ----------
async def cmd_diagnose(session):
    cfg = await config_dir(session)
    pdir = f"{cfg}/plugins/{PLUGIN_ID}"
    checks = {
        "plugin dir exists": f"app.vault.adapter.exists({js(pdir)})",
        "files in plugin dir": f"app.vault.adapter.list({js(pdir)}).catch(()=>null)",
        "manifest known to obsidian": f"app.plugins.manifests[{js(PLUGIN_ID)}] ?? '(not loaded)'",
        "is enabled": f"Array.from(app.plugins.enabledPlugins).includes({js(PLUGIN_ID)})",
        "is instantiated": f"!!app.plugins.plugins[{js(PLUGIN_ID)}]",
        "loaded version": f"app.plugins.plugins[{js(PLUGIN_ID)}]?.manifest?.version ?? null",
    }
    print(f"# diagnose ({pdir})\n")
    for label, expr in checks.items():
        try:
            res = await ev(session, expr)
        except Exception as e:  # noqa: BLE001
            res = f"<error: {e}>"
        print(f"## {label}\n{json.dumps(res, indent=2, ensure_ascii=False)}\n")


async def cmd_deploy(lockdown):
    # 1) transfer files via AFC
    afc = await afc_open(lockdown)
    try:
        vault = await afc_find_vault(afc, prefer="notes")
        pdir = f"{vault}/.obsidian/plugins/{PLUGIN_ID}"
        print(f"-> AFC target: {pdir}")
        try:
            await afc.makedirs(pdir)
        except Exception:  # noqa: BLE001
            pass  # already exists

        files = dict(LOCAL_FILES)
        if os.path.isfile(STYLES_SRC):
            files["styles.css"] = STYLES_SRC

        for name, path in files.items():
            with open(path, "rb") as f:
                data = f.read()
            got = await afc_put(afc, f"{pdir}/{name}", data)
            flag = "OK" if got == len(data) else f"MISMATCH (want {len(data)})"
            print(f"   pushed {name:13} {got}B  {flag}")

        # .hotreload marker so pjeby Hot Reload keeps it fresh on later pushes
        await afc_put(afc, f"{pdir}/.hotreload", b"")
    finally:
        await afc.close()

    # 2) load + enable via inspector
    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()
    try:
        async with inspector:
            _, session = await open_session(inspector)
            await ev(session, "app.plugins.loadManifests()")
            print("   manifest now loaded:",
                  await ev(session, f"app.plugins.manifests[{js(PLUGIN_ID)}]?.version ?? '(still not loaded)'"))
            res = await _enable(session)
            print("   enable result:", json.dumps(res, indent=2, ensure_ascii=False))
            if res and res.get("ok") and res.get("instantiated"):
                print("\n✅ deployed & loaded. If it didn't crash on load, run `logs` and reproduce.")
            else:
                print("\n⚠️  load reported a problem above — that may BE the crash. Also run `logs`.")
    finally:
        await inspector.close()


async def _enable(session):
    return await ev(session, f"""(async()=>{{
        try {{
            if (app.plugins.plugins[{js(PLUGIN_ID)}]) await app.plugins.disablePlugin({js(PLUGIN_ID)});
            await (app.plugins.enablePluginAndSave
                   ? app.plugins.enablePluginAndSave({js(PLUGIN_ID)})
                   : app.plugins.enablePlugin({js(PLUGIN_ID)}));
            return {{ok:true, instantiated: !!app.plugins.plugins[{js(PLUGIN_ID)}],
                     version: app.plugins.plugins[{js(PLUGIN_ID)}]?.manifest?.version ?? null}};
        }} catch (e) {{ return {{ok:false, error:String((e&&e.stack)||e)}}; }}
    }})()""")


async def cmd_reload(session):
    res = await ev(session, f"""(async()=>{{
        try {{
            if (app.plugins.plugins[{js(PLUGIN_ID)}]) await app.plugins.disablePlugin({js(PLUGIN_ID)});
            await app.plugins.enablePlugin({js(PLUGIN_ID)});
            return {{ok:true, instantiated: !!app.plugins.plugins[{js(PLUGIN_ID)}]}};
        }} catch (e) {{ return {{ok:false, error:String((e&&e.stack)||e)}}; }}
    }})()""")
    print(json.dumps(res, indent=2, ensure_ascii=False))


async def cmd_repro(session, max_wait=240, count=1):
    ep = await ev(session, "(()=>{const p=app.plugins.plugins.podnotes;"
                           "const e=p?.api?.podcast||p?.settings?.currentEpisode;"
                           "return e?{title:e.title,durationSec:e.duration,streamUrl:e.streamUrl}:null})()")
    if not ep:
        print("No current episode set. Open PodNotes on the phone and start an episode first.")
        return False
    hours = (ep.get("durationSec") or 0) / 3600
    print(f"Current episode: {ep['title']}  (~{hours:.1f}h)\n  {ep['streamUrl']}")
    print(f"\nFiring podnotes:download-playing-episode x{count} (concurrent, fire-and-forget)...")
    try:
        await asyncio.wait_for(
            session.runtime_evaluate(
                "(()=>{for(let i=0;i<%d;i++){app.commands.executeCommandById('podnotes:download-playing-episode');}"
                "return 'fired'})()" % count,
                return_by_value=True),
            timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"  (fire call returned {type(e).__name__} — downloads likely already running)")

    print("Watching app liveness (OOM => becomes unreachable):")
    elapsed, interval = 0, 3
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval
        try:
            await asyncio.wait_for(session.runtime_evaluate("1", return_by_value=True), timeout=6)
            print(f"  +{elapsed:>3}s  alive")
        except Exception as e:  # noqa: BLE001
            print(f"\n💥 Obsidian became UNREACHABLE after ~{elapsed}s ({type(e).__name__}) "
                  f"— killed mid-download. Check crash logs for the JetsamEvent.")
            return True
    print(f"\nStill alive after {max_wait}s — episode may have fit in memory; try a larger one.")
    return False


async def cmd_verify(session, max_wait=360):
    print("Firing a SINGLE download; watching on-disk size grow (proves chunked append):")
    await session.runtime_evaluate(
        "(()=>{app.commands.executeCommandById('podnotes:download-playing-episode');return 'fired'})()",
        return_by_value=True)
    probe = """(async()=>{const a=app.vault.adapter;let file=null,size=null;
      try{ if(await a.exists('PodNotes')){ const top=await a.list('PodNotes');
        for(const f of top.folders){ const sub=await a.list(f);
          const m=sub.files.find(x=>/\\.(mp3|m4a|mp4|m4v|mov)$/.test(x));
          if(m){file=m;size=(await a.stat(m)).size;break;} } } }catch(e){}
      const n=[...document.querySelectorAll('.notice')].map(x=>x.innerText).filter(t=>/Download|MB|%|Success/.test(t));
      return {file,size,notice:(n[n.length-1]||null)};})()"""
    last, stable, elapsed = -1, 0, 0
    while elapsed < max_wait:
        await asyncio.sleep(3)
        elapsed += 3
        try:
            info = await ev(session, probe)
        except Exception as e:  # noqa: BLE001
            print(f"  +{elapsed:>3}s  <app unreachable: {type(e).__name__}> — would mean a crash!")
            return False
        size = info.get("size") or 0
        mb = f"{size/1048576:6.1f}MB"
        print(f"  +{elapsed:>3}s  disk={mb}  notice={info.get('notice')!r}")
        if size and size == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = size
    final = await ev(session, probe)
    alive = await ev(session, "1") == 1
    print(f"\nFinal: {json.dumps(final)}  app_alive={alive}")
    return final


async def cmd_logs(session, seconds):
    # surface uncaught errors + promise rejections into the console stream
    await ev(session, """((w)=>{
        if (w.__pmd_hooked) return 'already';
        w.__pmd_hooked = true;
        w.addEventListener('error', e => console.error('[window.onerror]', e.message,
            (e.filename||'')+':'+(e.lineno||''), e.error && e.error.stack || ''));
        w.addEventListener('unhandledrejection', e => console.error('[unhandledrejection]',
            (e.reason && (e.reason.stack || e.reason)) || e.reason));
        return 'hooked';
    })(window)""")
    await session.console_enable()
    logging.getLogger("webinspector.console").setLevel(logging.DEBUG)
    print(f"-- streaming console + errors for {seconds}s (reproduce the crash now) --")
    await asyncio.sleep(seconds)


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("diagnose")
    sub.add_parser("deploy")
    sub.add_parser("reload")
    pr = sub.add_parser("repro")
    pr.add_argument("--count", type=int, default=1)
    sub.add_parser("verify")
    pe = sub.add_parser("eval")
    pe.add_argument("expr")
    pl = sub.add_parser("logs")
    pl.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args()

    lockdown = await create_using_usbmux()

    if args.cmd == "deploy":
        await cmd_deploy(lockdown)
        return

    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()
    try:
        async with inspector:
            _, session = await open_session(inspector)
            if args.cmd == "diagnose":
                await cmd_diagnose(session)
            elif args.cmd == "reload":
                await cmd_reload(session)
            elif args.cmd == "repro":
                await cmd_repro(session, count=args.count)
            elif args.cmd == "verify":
                await cmd_verify(session)
            elif args.cmd == "eval":
                print(json.dumps(await ev(session, args.expr), indent=2, ensure_ascii=False))
            elif args.cmd == "logs":
                await cmd_logs(session, args.seconds)
    finally:
        await inspector.close()


if __name__ == "__main__":
    asyncio.run(main())
