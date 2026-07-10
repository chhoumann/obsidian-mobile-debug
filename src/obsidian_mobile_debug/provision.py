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


def plan_writes(skeleton: list[VaultFile], existing_relpaths: set[str]) -> list[VaultFile]:
    """Filter the skeleton down to the files a (possibly re-run) provision should write.

    An ``overwrite`` file is always written; every other file is written only when
    it is absent from ``existing_relpaths`` - so a second run fills gaps left by a
    partial first run and never touches files already on the device.
    """
    return [f for f in skeleton if f.overwrite or f.relpath not in existing_relpaths]
