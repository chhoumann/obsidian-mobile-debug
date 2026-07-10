"""Scratch-vault provisioning shared by the iOS and Android platforms.

Mirrors the desktop runner's provision step (obsidian-e2e ``provisionVault``): lay
down a minimal ``.obsidian`` skeleton so a disposable vault can be opened, a
plugin loaded, and a fix verified on-device without touching real user data.

Everything here is pure logic - it computes *what* to write, never *how*. The
platform module (ios/android) supplies the filesystem: it reports which vault
files already exist, then applies the write plan through AFC (iOS) or ``adb``
(Android). That split keeps the skeleton content, path derivation, guard
interactions, and idempotency decisions unit-testable with a fake fs layer.

Idempotency mirrors the desktop runner's ``writeJsonIfMissing`` semantics: a
re-run only fills in missing files and never overwrites an existing ``data.json``
(the user's seeded document). ``community-plugins.json`` is the one file written
unconditionally when a plugin is provisioned, so the enable list survives a
partial prior run - matching the desktop runner exactly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# A vault whose name contains one of these tokens is a disposable test vault.
# Kept in sync with the same tuple in ios.py / android.py (the deploy/reload
# guard); provisioning reuses the identical rule so one safe-name definition
# governs every write path.
SAFE_VAULT_TOKENS = ("test", "scratch", "debug", "sandbox")

# Default scratch-vault name. Contains "scratch", so it always clears the
# safe-vault guard without --confirm-real-vault.
DEFAULT_VAULT = "omd-scratch"

# On-device roots. iOS vaults live in the app's house_arrest documents
# container; Android's emulator layout puts a scratch vault under Documents.
IOS_DOCUMENTS_ROOT = "/Documents"
ANDROID_DEFAULT_ROOT = "/storage/emulated/0/Documents"

CONFIG_DIR = ".obsidian"


@dataclass(frozen=True)
class VaultFile:
    """One file in the vault skeleton.

    ``relpath`` is POSIX, relative to the vault root (e.g.
    ``.obsidian/app.json``). ``overwrite`` picks the idempotency rule: ``True``
    always writes (community-plugins.json), ``False`` writes only when the file
    is absent (everything else, so a re-run never clobbers seeded data).
    """
    relpath: str
    content: bytes
    overwrite: bool


def _json_bytes(value: object) -> bytes:
    """Serialize like the desktop runner's writeJson: tab indent, trailing newline."""
    return (json.dumps(value, indent="\t") + "\n").encode("utf-8")


def looks_like_test_vault(name: str | None, expected: str | None = None) -> bool:
    if not name:
        return False
    if expected and name == expected:
        return True
    lowered = name.lower()
    return any(token in lowered for token in SAFE_VAULT_TOKENS)


def guard_provision_vault(vault_name: str, *, confirm_real: bool, test_vault: str | None) -> None:
    """Refuse to provision into a non-test-named vault without --confirm-real-vault."""
    if confirm_real or looks_like_test_vault(vault_name, test_vault):
        return
    raise SystemExit(
        f"Refusing to provision vault {vault_name!r}: it does not look like a scratch vault "
        f"(name contains none of {SAFE_VAULT_TOKENS}). Provisioning writes an .obsidian "
        f"skeleton into it.\nRe-run with --confirm-real-vault to proceed, or choose a safe "
        f"name like {DEFAULT_VAULT!r}."
    )


def guard_remove_vault(vault_name: str) -> None:
    """Refuse to remove anything but a safe-named scratch vault.

    Removal deletes the whole vault directory, so it is scratch-only by design:
    --confirm-real-vault deliberately does NOT unlock it. A real vault can never
    be removed by this tool.
    """
    if looks_like_test_vault(vault_name):
        return
    raise SystemExit(
        f"Refusing to remove vault {vault_name!r}: removal is scratch-only and its name "
        f"contains none of {SAFE_VAULT_TOKENS}. This guard has no override - rename the "
        f"vault or delete it by hand if you really mean to."
    )


def workspace_skeleton(plugin_id: str | None) -> dict[str, object]:
    """Minimal split workspace, ids namespaced so they never collide with a real layout."""
    base = f"{plugin_id or 'omd'}-scratch"
    return {
        "main": {"id": base, "type": "split", "children": []},
        "left": {"id": f"{base}-left", "type": "split", "children": []},
        "right": {"id": f"{base}-right", "type": "split", "children": []},
    }


def vault_skeleton(plugin_id: str | None, data_seed: bytes | None) -> list[VaultFile]:
    """The full set of skeleton files for a scratch vault.

    Mirrors the desktop runner: app.json / appearance.json / core-plugins.json /
    workspace.json are write-if-missing; community-plugins.json enables the plugin
    and is written unconditionally so the enable list is authoritative. A plugin's
    data.json is seeded (write-if-missing) only when --data is supplied - the
    plugin's own artifacts are pushed separately by the deploy composition.
    """
    files = [
        VaultFile(f"{CONFIG_DIR}/app.json", _json_bytes({}), overwrite=False),
        VaultFile(f"{CONFIG_DIR}/appearance.json", _json_bytes({}), overwrite=False),
        VaultFile(f"{CONFIG_DIR}/core-plugins.json", _json_bytes([]), overwrite=False),
        VaultFile(
            f"{CONFIG_DIR}/community-plugins.json",
            _json_bytes([plugin_id] if plugin_id else []),
            overwrite=True,
        ),
        VaultFile(f"{CONFIG_DIR}/workspace.json", _json_bytes(workspace_skeleton(plugin_id)), overwrite=False),
    ]
    if plugin_id and data_seed is not None:
        files.append(
            VaultFile(f"{CONFIG_DIR}/plugins/{plugin_id}/data.json", data_seed, overwrite=False)
        )
    return files


# Mobile Obsidian records the open vault as an absolute path in these localStorage
# keys (discovered by reading app.openVaultChooser on-device): the selected vault
# and the registry of known external vaults. Switching vaults = register the path,
# select it, and reload. app.openVaultChooser itself only clears the selection to
# show the chooser UI; there is no by-name "open this vault" API, so this is the
# scriptable path. Shared by both platforms (same Capacitor mobile bundle).
SELECTED_VAULT_KEY = "mobile-selected-vault"
EXTERNAL_VAULTS_KEY = "mobile-external-vaults"

CURRENT_SELECTED_VAULT_JS = f'localStorage.getItem({json.dumps(SELECTED_VAULT_KEY)})'


def open_vault_js(vault_abs_path: str) -> str:
    """JS that registers + selects an on-device vault path and reloads into it."""
    path = json.dumps(vault_abs_path)
    return (
        "(() => {"
        f"const p = {path};"
        f"const reg = JSON.parse(localStorage.getItem({json.dumps(EXTERNAL_VAULTS_KEY)}) || '[]');"
        "if (!reg.includes(p)) reg.push(p);"
        f"localStorage.setItem({json.dumps(EXTERNAL_VAULTS_KEY)}, JSON.stringify(reg));"
        f"localStorage.setItem({json.dumps(SELECTED_VAULT_KEY)}, p);"
        "setTimeout(() => location.reload(), 300);"
        "return { opened: p, registered: reg };"
        "})()"
    )


def derive_sibling_vault_path(current_selected: str | None, vault_name: str) -> str:
    """Absolute path of a sibling vault next to the currently-open one.

    iOS vaults live at a sandbox-absolute path (``/var/mobile/Containers/.../Documents/<vault>``)
    that AFC's ``/Documents`` view does not reveal. The open vault's recorded path
    ends in its own name, so its parent is the container Documents dir; the new
    scratch vault is a sibling there.
    """
    if not current_selected:
        raise SystemExit(
            "Cannot derive the on-device vault path to open: no vault is currently selected "
            f"(localStorage {SELECTED_VAULT_KEY!r} is empty). Open any vault in the app once, "
            "then rerun with --open - or open the scratch vault by hand."
        )
    parent = current_selected.rsplit("/", 1)[0]
    return f"{parent}/{vault_name}"


def open_hint(open_flag: bool, plugin_id: str | None, platform: str) -> str:
    """Human-facing next-step hint for the provision report's openVaultHint field."""
    plugin_step = (
        f" Then enable the plugin with `omd {platform} reload --plugin {plugin_id}` "
        "(it disables Restricted Mode and instantiates the plugin)."
        if plugin_id else ""
    )
    if open_flag:
        return "Obsidian is reloading into the scratch vault now (a few seconds)." + plugin_step
    return (
        "Rerun with --open to switch Obsidian into this vault automatically, or open it by hand "
        "in the app (vault switcher -> Manage vaults / Open folder as vault)." + plugin_step
    )


def plan_writes(skeleton: list[VaultFile], existing_relpaths: set[str]) -> list[VaultFile]:
    """Filter the skeleton down to the files a (possibly re-run) provision should write.

    An ``overwrite`` file is always written; every other file is written only when
    it is absent from ``existing_relpaths`` - so a second run fills gaps left by a
    partial first run and never touches files already on the device.
    """
    return [f for f in skeleton if f.overwrite or f.relpath not in existing_relpaths]
