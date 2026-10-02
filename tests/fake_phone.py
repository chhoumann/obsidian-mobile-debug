"""A fake device running Obsidian, for vault-deletion safety tests.

It models the two facts deletion safety depends on: which vault path Obsidian
has selected (localStorage) and which vault dirs exist on disk. The command
under test runs for real against it.
"""
import contextlib
import json
import re

from obsidian_mobile_debug import provision as prov

SCRATCH = "quickadd-omd-scratch"
IOS_SCRATCH_DIR = f"/Documents/{SCRATCH}"
ANDROID_ROOT = "/storage/emulated/0/Documents"
ANDROID_SCRATCH_DIR = f"{ANDROID_ROOT}/{SCRATCH}"
ANDROID_NAME_JS = "app?.vault?.getName?.() ?? null"


class FakePhone:
    def __init__(self, selected, dirs, loaded=None):
        self.selected = selected
        self.dirs = set(dirs)
        # A vault still loaded after its localStorage selection was cleared.
        self.loaded = loaded

    @property
    def vault_name(self):
        if self.loaded:
            return self.loaded
        return self.selected.rstrip("/").rsplit("/", 1)[-1] if self.selected else None

    def ev(self, expr):
        if expr == prov.CURRENT_SELECTED_VAULT_JS:
            return self.selected
        if expr == ANDROID_NAME_JS:
            return self.vault_name
        if "location.reload" in expr:  # open_vault_js: select the path and reload
            self.selected = json.loads(re.search(r'const p = ("[^"]*");', expr).group(1))
            return {"opened": self.selected}
        return {"ok": True}

    def runtime(self):
        return {"vaultName": self.vault_name, "plugin": {"enabled": True, "instantiated": True}}


def outcome(coro):
    import asyncio

    try:
        return asyncio.run(coro)
    except SystemExit as exc:
        return exc


def fake_ios(monkeypatch, phone):
    from obsidian_mobile_debug import ios, lock

    class _Session:
        async def console_enable(self):
            pass

    @contextlib.asynccontextmanager
    async def session_cm(_lockdown, _bundle):
        yield "target", _Session()

    class _AFC:
        async def exists(self, path):
            return path in phone.dirs

        async def rm(self, path, force=False):
            phone.dirs.discard(path)
            return []

        async def close(self):
            pass

    async def afc_open(_lockdown, _bundle):
        return _AFC()

    async def provision_scratch_vault(_afc, name, *_rest):
        phone.dirs.add(f"/Documents/{name}")
        return {"plugin": {"pushed": {"main.js": {"ok": True}}}}

    async def ev(_session, expr, timeout=30.0):
        return phone.ev(expr)

    async def read_vault_identity(_session):
        return prov.vault_identity(phone.vault_name, phone.selected)

    async def read_runtime_state(_session, _plugin):
        return phone.runtime()

    async def enable_plugin(_session, _plugin):
        return {}

    monkeypatch.setattr(lock, "inspector_lock", lambda *_a: contextlib.nullcontext())
    monkeypatch.setattr(ios, "inspector_session_unlocked", session_cm)
    monkeypatch.setattr(ios, "afc_open", afc_open)
    monkeypatch.setattr(ios, "provision_scratch_vault", provision_scratch_vault)
    monkeypatch.setattr(ios, "resolve_plugin_files", lambda _args: {})
    monkeypatch.setattr(ios, "ev", ev)
    monkeypatch.setattr(ios, "read_vault_identity", read_vault_identity)
    monkeypatch.setattr(ios, "read_runtime_state", read_runtime_state)
    monkeypatch.setattr(ios, "enable_plugin", enable_plugin)
    monkeypatch.setattr(ios, "install_console_capture", lambda *_a: None)


def fake_android(monkeypatch, phone):
    from obsidian_mobile_debug import android

    @contextlib.contextmanager
    def cdp_forward(_port, _package):
        yield 4242

    async def ev(_port, expr, *, timeout=120.0, await_promise=True):
        return phone.ev(expr)

    async def ev_with_console(_port, expr, *, timeout=120.0, events=None):
        return phone.ev(expr), []

    async def read_runtime_state(_port, _plugin):
        return phone.runtime()

    async def enable_plugin(_port, _plugin):
        return {}

    def adb_out(args, *, check=True):
        if args[:3] == ["shell", "ls", "-d"]:
            return args[3] if args[3] in phone.dirs else ""
        return ""

    def run_adb(args, *, check=True):
        if args[:3] == ["shell", "rm", "-rf"]:
            phone.dirs.discard(args[3])

    def write_device_file(path, _content):
        phone.dirs.add(path.split("/.obsidian/")[0])

    monkeypatch.setattr(android, "cdp_forward", cdp_forward)
    monkeypatch.setattr(android, "ev", ev)
    monkeypatch.setattr(android, "ev_with_console", ev_with_console)
    monkeypatch.setattr(android, "read_runtime_state", read_runtime_state)
    monkeypatch.setattr(android, "enable_plugin", enable_plugin)
    monkeypatch.setattr(android, "resolve_plugin_files_for_provision", lambda _a: {"main.js": "m"})
    monkeypatch.setattr(android, "existing_vault_files", lambda _path: set())
    monkeypatch.setattr(android, "write_device_file", write_device_file)
    monkeypatch.setattr(android, "push_plugin_files", lambda *_a: None)
    monkeypatch.setattr(android, "adb_out", adb_out)
    monkeypatch.setattr(android, "run_adb", run_adb)

