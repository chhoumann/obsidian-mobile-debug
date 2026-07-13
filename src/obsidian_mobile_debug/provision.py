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


def sanitize_vault_segment(value: str) -> str:
    """A plugin id reduced to a stable, path-safe, lowercase name segment.

    Collapses whitespace and anything outside [a-z0-9_.-] to single dashes so
    names with spaces or path characters ("My Plugin!", "a/b") stay valid vault
    folder names and derive the same segment on every rerun.
    """
    import re

    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.")


def resolve_vault_name(explicit: str | None, plugin_id: str | None) -> tuple[str, str]:
    """The scratch-vault name to use, plus where it came from.

    Returns ``(name, source)`` with source one of ``explicit`` (the user passed
    --vault; behavior unchanged), ``derived`` (namespaced to the plugin, e.g.
    ``quickadd-omd-scratch``, so two plugins' scratch vaults never collide on
    the display name), or ``default`` (plugin-agnostic ``omd-scratch``).

    Every derived name ends in ``-{DEFAULT_VAULT}``, which contains "scratch",
    so it always stays inside the scratch-name safety guard. A plugin id that
    sanitizes to nothing falls back to the plain default.
    """
    if explicit:
        return explicit, "explicit"
    if plugin_id:
        segment = sanitize_vault_segment(plugin_id)
        if segment:
            return f"{segment}-{DEFAULT_VAULT}", "derived"
    return DEFAULT_VAULT, "default"

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

# Per-vault community-plugins trust flag ("Restricted Mode" off), keyed by the
# vault's recorded path. Obsidian mobile shows a manual Trust prompt on first
# open of an unknown vault whose community-plugins.json lists plugins; setting
# this key BEFORE the switch is what pressing Trust would have written, so a
# provisioned scratch vault opens hands-off. (Discovered by dumping
# localStorage after pressing Trust on-device.)
TRUST_KEY_PREFIX = "enable-plugin-"


def trust_vault_js(vault_path: str) -> str:
    return f'localStorage.setItem({json.dumps(TRUST_KEY_PREFIX + vault_path)}, "true")'


def forget_vault_js(vault_path: str) -> str:
    """JS that deregisters a (removed) vault and drops its trust flag.

    Without this, a deleted scratch vault keeps a dead entry in the vault
    switcher (the external-vaults registry) and a stale trust key.
    """
    path = json.dumps(vault_path)
    return (
        "(() => {"
        f"const p = {path};"
        f"const reg = JSON.parse(localStorage.getItem({json.dumps(EXTERNAL_VAULTS_KEY)}) || '[]')"
        ".filter((entry) => entry !== p);"
        f"localStorage.setItem({json.dumps(EXTERNAL_VAULTS_KEY)}, JSON.stringify(reg));"
        f"localStorage.removeItem({json.dumps(TRUST_KEY_PREFIX)} + p);"
        "return { forgot: p, registered: reg };"
        "})()"
    )


def open_vault_js(vault_abs_path: str, trust_plugins: bool = False) -> str:
    """JS that registers + selects an on-device vault path and reloads into it.

    ``trust_plugins`` pre-trusts the vault (skips the Restricted Mode prompt).
    Only pass it for OMD-provisioned scratch vaults - restoring a user's own
    vault must never override their trust decision.
    """
    path = json.dumps(vault_abs_path)
    trust = f"{trust_vault_js(vault_abs_path)};" if trust_plugins else ""
    return (
        "(() => {"
        f"const p = {path};"
        f"const reg = JSON.parse(localStorage.getItem({json.dumps(EXTERNAL_VAULTS_KEY)}) || '[]');"
        "if (!reg.includes(p)) reg.push(p);"
        f"localStorage.setItem({json.dumps(EXTERNAL_VAULTS_KEY)}, JSON.stringify(reg));"
        f"localStorage.setItem({json.dumps(SELECTED_VAULT_KEY)}, p);"
        f"{trust}"
        "setTimeout(() => location.reload(), 300);"
        "return { opened: p, registered: reg };"
        "})()"
    )


# The AFC house_arrest (documents_only) view we provision through maps onto the
# app's own sandbox container: /var/mobile/Containers/Data/Application/<UUID>/Documents.
# This marker identifies a vault that lives in that container - the only case where
# the provisioned /Documents/<vault> is a genuine sibling of the open vault. An
# iCloud/external vault records a different root (e.g. .../Mobile Documents/...),
# so a sibling of it would point nowhere the provision wrote.
APP_CONTAINER_MARKER = "/Containers/Data/Application/"

# iCloud Drive vault roots recorded by mobile Obsidian: the classic
# "Mobile Documents" container path and the CloudDocs bundle id form.
ICLOUD_MARKERS = ("/Mobile Documents/", "com~apple~CloudDocs")


def classify_storage(selected_path: str | None) -> str:
    """Storage kind of a vault from its recorded localStorage path.

    A vault display name is not a unique mobile vault identity: two vaults named
    ``omd-scratch`` can be backed by the app container and by iCloud at the same
    time. The recorded localStorage path is what tells them apart:

    - ``app-container``: the app's own sandbox (the only storage AFC
      house_arrest provisioning can reach). Recorded either relative to the
      sandbox root (``documents/<vault>``, observed on iOS 18 Obsidian) or as
      an absolute /var/mobile/Containers/Data/Application/<UUID>/... path,
    - ``icloud``: an iCloud Drive container,
    - ``external``: any other on-device location (Files-app folder, etc.),
    - ``unknown``: no path recorded (no vault selected yet).
    """
    if not selected_path:
        return "unknown"
    if APP_CONTAINER_MARKER in selected_path or _relative_documents_parent(selected_path):
        return "app-container"
    if any(marker in selected_path for marker in ICLOUD_MARKERS):
        return "icloud"
    return "external"


def _relative_documents_parent(selected_path: str) -> str | None:
    """The ``documents`` prefix (as recorded) of a sandbox-relative vault path.

    Mobile Obsidian records an app-container vault as ``documents/<vault>``;
    returns that parent segment when the path has this shape, else None.
    """
    if selected_path.startswith("/"):
        return None
    parent, _, name = selected_path.rpartition("/")
    if name and parent.lower() == "documents":
        return parent
    return None


def vault_identity(vault_name: str | None, selected_path: str | None) -> dict[str, object]:
    """The full identity of the runtime vault: name + backing storage."""
    return {
        "vaultName": vault_name,
        "selectedVaultPath": selected_path,
        "storageKind": classify_storage(selected_path),
    }


def describe_vault_identity(identity: dict[str, object]) -> str:
    """One-line human form: name, storage kind, and backing path."""
    name = identity.get("vaultName")
    path = identity.get("selectedVaultPath")
    return f"{name!r} ({identity.get('storageKind')}, {path or 'no recorded path'})"


def afc_vault_corresponds(selected_path: str | None, afc_vault_name: str) -> bool:
    """Whether the AFC vault at /Documents/<name> is the vault Obsidian has open.

    True only when the open vault lives in the app container (the storage AFC
    writes to) AND its path basename matches the AFC folder name. A same-name
    iCloud/external vault fails this even though the display names collide.
    """
    return (
        classify_storage(selected_path) == "app-container"
        and selected_path is not None
        and selected_path.rstrip("/").rsplit("/", 1)[-1] == afc_vault_name
    )


def derive_sibling_vault_path(current_selected: str | None, vault_name: str) -> str:
    """Recorded path of a sibling vault next to the currently-open one.

    An app-container vault's recorded path ends in its own name, so its parent
    is the container Documents dir and the new scratch vault is a sibling
    there. Obsidian records that in two shapes, both preserved here: relative
    ``documents/<vault>`` (observed on iOS 18) and sandbox-absolute
    ``/var/mobile/Containers/.../Documents/<vault>`` (which AFC's
    ``/Documents`` view does not reveal).

    Only valid when the open vault is itself in the app container: provisioning
    writes through AFC into that container's ``Documents``, so a sibling of an
    iCloud/external vault would resolve to a path the provision never touched.
    Refuse in that case rather than reload Obsidian into an empty vault.
    """
    if not current_selected:
        raise SystemExit(
            "Cannot derive the on-device vault path to open: no vault is currently selected "
            f"(localStorage {SELECTED_VAULT_KEY!r} is empty). Open any vault in the app once, "
            "then rerun with --open - or open the scratch vault by hand."
        )
    relative_parent = _relative_documents_parent(current_selected)
    if relative_parent is not None:
        return f"{relative_parent}/{vault_name}"
    parent = current_selected.rsplit("/", 1)[0]
    if APP_CONTAINER_MARKER not in current_selected or not parent.endswith("/Documents"):
        raise SystemExit(
            f"Cannot open the scratch vault automatically: the vault currently open in Obsidian "
            f"is {classify_storage(current_selected)}-backed ({current_selected!r}), not in the "
            f"app's Documents container, so the provisioned vault at .../Documents/{vault_name} "
            "is not a sibling of it and --open would reload into an empty vault. The scratch "
            "vault was still provisioned - open it by hand in the app (vault switcher), or first "
            "open any in-app (non-iCloud/external) vault and rerun with --open."
        )
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
