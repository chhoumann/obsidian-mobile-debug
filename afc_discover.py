import asyncio
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.house_arrest import HouseArrestService

async def safe_ls(afc, p):
    try:
        return await afc.listdir(p)
    except Exception as e:
        return f"<err: {e}>"

async def main():
    ld = await create_using_usbmux()
    afc = await HouseArrestService.create(ld, "md.obsidian", documents_only=True)
    try:
        for root in ["/", "/Documents", "."]:
            print(f"{root!r:14} -> {await safe_ls(afc, root)}")
        # try to locate .obsidian under each top-level vault folder
        for base in ["/Documents", "/"]:
            entries = await safe_ls(afc, base)
            if isinstance(entries, list):
                for e in entries:
                    if e in (".", ".."):
                        continue
                    sub = f"{base.rstrip('/')}/{e}"
                    obs = await safe_ls(afc, f"{sub}/.obsidian")
                    if isinstance(obs, list):
                        print(f"\nVAULT: {sub}  (.obsidian present)")
                        pod = await safe_ls(afc, f"{sub}/.obsidian/plugins/podnotes")
                        print(f"  podnotes -> {pod}")
                break
    finally:
        await afc.close()

asyncio.run(main())
