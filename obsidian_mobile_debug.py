#!/usr/bin/env python3
"""Debug Obsidian (or any WKWebView app) on a USB-connected iPhone, over USB.

Generic, config-driven CLI built on pymobiledevice3:
- WebKit Web Inspector  -> evaluate JS against the page (app.* API), enable/reload plugins
- AFC / house_arrest    -> push a locally-built plugin into the vault

This is the generalized version of podnotes_ios.py — nothing is hardcoded to a
specific plugin; pass --bundle / --plugin / --repo.

Run with:
  uv run --no-project --with pymobiledevice3 python obsidian_mobile_debug.py <cmd> ...
  # or the uv-tool interpreter:
  ~/.local/share/uv/tools/pymobiledevice3/bin/python obsidian_mobile_debug.py <cmd> ...

Examples:
  ... pages
  ... eval 'app.vault.getName()'
  ... diagnose --plugin dataview
  ... deploy  --plugin dataview --repo ~/Developer/dataview
  ... reload  --plugin dataview
  ... command app:reload
  ... logs --seconds 60
"""
import argparse
import asyncio
import json
import logging
import os

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.webinspector import WebinspectorService

DEFAULT_BUNDLE = "md.obsidian"
TOKEN = "__omd_r"


# ---------- inspector connection ----------
async def open_session(inspector, bundle, name_match=None):
    pages = await inspector.get_open_application_pages(timeout=3)

    def matches(ap):
        if ap.application.bundle == bundle:
            return True
        name = (ap.application.name or "").lower()
        return bool(name_match) and name_match.lower() in name

    target = next((ap for ap in pages if matches(ap)), None)
    if target is None:
        raise SystemExit(
            f"No inspectable page for bundle {bundle!r}. Unlock the phone, open the "
            f"app, and ensure Settings > Apps > Safari > Advanced > Web Inspector is ON.\n"
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


# ---------- AFC file transfer (house_arrest documents container) ----------
async def afc_open(lockdown, bundle):
    return await HouseArrestService.create(lockdown, bundle, documents_only=True)


async def afc_find_vault(afc, prefer=None):
    candidates = []
    for name in await afc.listdir("/Documents"):
        if name in (".", ".."):
            continue
        try:
            if ".obsidian" in await afc.listdir(f"/Documents/{name}"):
                candidates.append(name)
        except Exception:  # noqa: BLE001
            continue
    if prefer:
        if prefer in candidates:
            return f"/Documents/{prefer}"
        raise SystemExit(f"Vault {prefer!r} not found. Vaults present: {candidates}")
    if len(candidates) == 1:
        return f"/Documents/{candidates[0]}"
    if not candidates:
        raise SystemExit("No Obsidian vault (.obsidian) found under the app's Documents.")
    raise SystemExit(f"Multiple vaults found; pass --vault one of: {candidates}")


async def afc_put(afc, remote_path, data: bytes):
    await afc.set_file_contents(remote_path, data)
    st = await afc.stat(remote_path)
    return int(st.get("st_size", st.get("size", -1)))


# ---------- plugin file resolution ----------
def resolve_plugin_files(args):
    """Map on-device filename -> local path. styles.css is included only if present."""
    if args.main:
        main = args.main
    elif args.repo:
        root = os.path.expanduser(args.repo)
        main = os.path.join(root, "main.js")
        if not os.path.exists(main):
            main = os.path.join(root, "build", "main.js")
    else:
        raise SystemExit("deploy needs --repo or explicit --main/--manifest")

    manifest = args.manifest or (os.path.join(os.path.expanduser(args.repo), "manifest.json") if args.repo else None)
    if not manifest:
        raise SystemExit("deploy needs --manifest (or --repo)")

    files = {"main.js": main, "manifest.json": manifest}
    styles = args.styles or (os.path.join(os.path.expanduser(args.repo), "styles.css") if args.repo else None)
    if styles and os.path.isfile(styles):
        files["styles.css"] = styles
    for name, path in files.items():
        if not os.path.isfile(path):
            raise SystemExit(f"missing build file for {name}: {path}")
    return files


# ---------- subcommands ----------
async def cmd_pages(inspector):
    for ap in await inspector.get_open_application_pages(timeout=3):
        print(ap)


async def cmd_eval(session, expr):
    print(json.dumps(await ev(session, expr), indent=2, ensure_ascii=False))


async def cmd_command(session, command_id):
    res = await ev(
        session,
        f"(()=>{{const ok=app.commands.executeCommandById({js(command_id)});return {{executed:ok}}}})()",
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))


async def cmd_diagnose(session, plugin):
    cfg = await config_dir(session)
    pdir = f"{cfg}/plugins/{plugin}"
    checks = {
        "plugin dir": f"app.vault.adapter.exists({js(pdir)})",
        "files": f"app.vault.adapter.list({js(pdir)}).catch(()=>null)",
        "manifest known": f"app.plugins.manifests[{js(plugin)}] ?? '(not loaded)'",
        "enabled": f"Array.from(app.plugins.enabledPlugins).includes({js(plugin)})",
        "instantiated": f"!!app.plugins.plugins[{js(plugin)}]",
        "loaded version": f"app.plugins.plugins[{js(plugin)}]?.manifest?.version ?? null",
    }
    print(f"# diagnose {plugin} ({pdir})\n")
    for label, expr in checks.items():
        try:
            res = await ev(session, expr)
        except Exception as e:  # noqa: BLE001
            res = f"<error: {e}>"
        print(f"## {label}\n{json.dumps(res, indent=2, ensure_ascii=False)}\n")


async def _enable(session, plugin):
    return await ev(session, f"""(async()=>{{
        try {{
            if (app.plugins.plugins[{js(plugin)}]) await app.plugins.disablePlugin({js(plugin)});
            await (app.plugins.enablePluginAndSave
                   ? app.plugins.enablePluginAndSave({js(plugin)})
                   : app.plugins.enablePlugin({js(plugin)}));
            return {{ok:true, instantiated: !!app.plugins.plugins[{js(plugin)}],
                     version: app.plugins.plugins[{js(plugin)}]?.manifest?.version ?? null}};
        }} catch (e) {{ return {{ok:false, error:String((e&&e.stack)||e)}}; }}
    }})()""")


async def cmd_reload(session, plugin):
    print(json.dumps(await _enable(session, plugin), indent=2, ensure_ascii=False))


async def cmd_logs(session, seconds):
    await ev(session, """((w)=>{
        if (w.__omd_hooked) return 'already';
        w.__omd_hooked = true;
        w.addEventListener('error', e => console.error('[onerror]', e.message,
            (e.filename||'')+':'+(e.lineno||''), e.error && e.error.stack || ''));
        w.addEventListener('unhandledrejection', e => console.error('[unhandledrejection]',
            (e.reason && (e.reason.stack || e.reason)) || e.reason));
        return 'hooked';
    })(window)""")
    await session.console_enable()
    logging.getLogger("webinspector.console").setLevel(logging.DEBUG)
    print(f"-- streaming console + errors for {seconds}s --")
    await asyncio.sleep(seconds)


async def cmd_deploy(lockdown, bundle, plugin, files, vault):
    # 1) transfer files via AFC
    afc = await afc_open(lockdown, bundle)
    try:
        vault_dir = await afc_find_vault(afc, prefer=vault)
        pdir = f"{vault_dir}/.obsidian/plugins/{plugin}"
        print(f"-> AFC target: {pdir}")
        try:
            await afc.makedirs(pdir)
        except Exception:  # noqa: BLE001
            pass
        for name, path in files.items():
            with open(path, "rb") as f:
                data = f.read()
            got = await afc_put(afc, f"{pdir}/{name}", data)
            flag = "OK" if got == len(data) else f"MISMATCH (want {len(data)})"
            print(f"   pushed {name:13} {got}B  {flag}")
        await afc_put(afc, f"{pdir}/.hotreload", b"")  # let pjeby Hot Reload watch it
    finally:
        await afc.close()

    # 2) load + enable via inspector
    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()
    try:
        async with inspector:
            _, session = await open_session(inspector, bundle)
            await ev(session, "app.plugins.loadManifests()")
            res = await _enable(session, plugin)
            print("   enable result:", json.dumps(res, indent=2, ensure_ascii=False))
            print("\n✅ deployed & loaded." if res and res.get("instantiated")
                  else "\n⚠️  load reported a problem (see above); run `logs`.")
    finally:
        await inspector.close()


# ---------- argparse ----------
def build_parser():
    ap = argparse.ArgumentParser(description="Debug Obsidian / WKWebView apps on iPhone over USB.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bundle", default=DEFAULT_BUNDLE, help=f"app bundle id (default {DEFAULT_BUNDLE})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pages", parents=[common], help="list inspectable pages/webviews")

    pe = sub.add_parser("eval", parents=[common], help="evaluate a JS expression")
    pe.add_argument("expr")

    pc = sub.add_parser("command", parents=[common], help="run an Obsidian command by id")
    pc.add_argument("command_id")

    pd = sub.add_parser("diagnose", parents=[common], help="report a plugin's install/enable state")
    pd.add_argument("--plugin", required=True)

    pr = sub.add_parser("reload", parents=[common], help="disable+enable a plugin")
    pr.add_argument("--plugin", required=True)

    pl = sub.add_parser("logs", parents=[common], help="stream console + uncaught errors")
    pl.add_argument("--seconds", type=int, default=60)

    pdep = sub.add_parser("deploy", parents=[common], help="AFC-push a built plugin + reload")
    pdep.add_argument("--plugin", required=True)
    pdep.add_argument("--repo", help="plugin repo path (derives main.js/manifest.json/styles.css)")
    pdep.add_argument("--main", help="path to built main.js (overrides --repo)")
    pdep.add_argument("--manifest", help="path to manifest.json (overrides --repo)")
    pdep.add_argument("--styles", help="path to styles.css (optional)")
    pdep.add_argument("--vault", help="vault folder name on device (auto-detected if single)")
    return ap


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args()
    lockdown = await create_using_usbmux()

    if args.cmd == "deploy":
        await cmd_deploy(lockdown, args.bundle, args.plugin, resolve_plugin_files(args), args.vault)
        return

    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()
    try:
        async with inspector:
            if args.cmd == "pages":
                await cmd_pages(inspector)
                return
            _, session = await open_session(inspector, args.bundle)
            if args.cmd == "eval":
                await cmd_eval(session, args.expr)
            elif args.cmd == "command":
                await cmd_command(session, args.command_id)
            elif args.cmd == "diagnose":
                await cmd_diagnose(session, args.plugin)
            elif args.cmd == "reload":
                await cmd_reload(session, args.plugin)
            elif args.cmd == "logs":
                await cmd_logs(session, args.seconds)
    finally:
        await inspector.close()


if __name__ == "__main__":
    asyncio.run(main())
