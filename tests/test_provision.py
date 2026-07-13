"""Provisioning pure logic: skeleton content, guards, idempotency planning."""
import json

import pytest

from obsidian_mobile_debug import provision as prov


def _by_path(files):
    return {f.relpath: f for f in files}


def test_default_vault_passes_safe_guard():
    # The default name must never trip the guard.
    prov.guard_provision_vault(prov.DEFAULT_VAULT, confirm_real=False, test_vault=None)
    assert prov.looks_like_test_vault(prov.DEFAULT_VAULT)


def test_skeleton_without_plugin():
    files = _by_path(prov.vault_skeleton(None, None))
    assert set(files) == {
        ".obsidian/app.json",
        ".obsidian/appearance.json",
        ".obsidian/core-plugins.json",
        ".obsidian/community-plugins.json",
        ".obsidian/workspace.json",
    }
    assert json.loads(files[".obsidian/app.json"].content) == {}
    assert json.loads(files[".obsidian/core-plugins.json"].content) == []
    assert json.loads(files[".obsidian/community-plugins.json"].content) == []


def test_skeleton_enables_plugin_in_community_plugins():
    files = _by_path(prov.vault_skeleton("metaedit", None))
    assert json.loads(files[".obsidian/community-plugins.json"].content) == ["metaedit"]
    # community-plugins.json is the one file written unconditionally.
    assert files[".obsidian/community-plugins.json"].overwrite is True
    assert files[".obsidian/app.json"].overwrite is False


def test_skeleton_workspace_ids_namespaced_by_plugin():
    files = _by_path(prov.vault_skeleton("metaedit", None))
    workspace = json.loads(files[".obsidian/workspace.json"].content)
    assert workspace["main"]["id"] == "metaedit-scratch"
    assert workspace["left"]["id"] == "metaedit-scratch-left"


def test_skeleton_data_seed_only_with_plugin():
    seeded = _by_path(prov.vault_skeleton("metaedit", b'{"k":1}'))
    entry = seeded[".obsidian/plugins/metaedit/data.json"]
    assert entry.content == b'{"k":1}'
    assert entry.overwrite is False  # never clobber a seeded document
    # A data seed without a plugin has nowhere to go and is dropped.
    assert not any("data.json" in f.relpath for f in prov.vault_skeleton(None, b"{}"))


def test_json_bytes_matches_desktop_runner_format():
    # Tab indent + trailing newline, like obsidian-e2e writeJson.
    files = _by_path(prov.vault_skeleton("metaedit", None))
    assert files[".obsidian/community-plugins.json"].content == b'[\n\t"metaedit"\n]\n'


def test_plan_writes_first_run_writes_everything():
    skeleton = prov.vault_skeleton("metaedit", None)
    planned = prov.plan_writes(skeleton, existing_relpaths=set())
    assert [f.relpath for f in planned] == [f.relpath for f in skeleton]


def test_plan_writes_skips_existing_but_keeps_overwrite():
    skeleton = prov.vault_skeleton("metaedit", None)
    existing = {
        ".obsidian/app.json",
        ".obsidian/community-plugins.json",
        ".obsidian/workspace.json",
    }
    planned = {f.relpath for f in prov.plan_writes(skeleton, existing)}
    # app.json + workspace.json already present -> skipped.
    assert ".obsidian/app.json" not in planned
    assert ".obsidian/workspace.json" not in planned
    # community-plugins.json is overwrite=True -> rewritten even though present.
    assert ".obsidian/community-plugins.json" in planned
    # A missing file is (re)written.
    assert ".obsidian/core-plugins.json" in planned


def test_plan_writes_never_overwrites_existing_data_json():
    skeleton = prov.vault_skeleton("metaedit", b'{"fresh":true}')
    existing = {".obsidian/plugins/metaedit/data.json"}
    planned = {f.relpath for f in prov.plan_writes(skeleton, existing)}
    assert ".obsidian/plugins/metaedit/data.json" not in planned


def test_guard_provision_blocks_real_vault():
    with pytest.raises(SystemExit):
        prov.guard_provision_vault("notes", confirm_real=False, test_vault=None)


def test_guard_provision_allows_confirm_flag():
    prov.guard_provision_vault("notes", confirm_real=True, test_vault=None)


def test_guard_provision_allows_whitelisted_name():
    prov.guard_provision_vault("notes", confirm_real=False, test_vault="notes")


def test_guard_remove_allows_scratch_name():
    prov.guard_remove_vault("omd-scratch")


def test_guard_remove_blocks_real_vault_even_with_confirm():
    # Removal is scratch-only by design: there is no --confirm-real-vault override,
    # so guard_remove_vault has no bypass parameter at all.
    with pytest.raises(SystemExit):
        prov.guard_remove_vault("notes")


def test_open_vault_js_registers_selects_and_reloads():
    src = prov.open_vault_js("/storage/emulated/0/Documents/omd-scratch")
    assert '"/storage/emulated/0/Documents/omd-scratch"' in src
    assert prov.SELECTED_VAULT_KEY in src
    assert prov.EXTERNAL_VAULTS_KEY in src
    assert "location.reload()" in src


def test_derive_sibling_vault_path_from_open_vault():
    got = prov.derive_sibling_vault_path(
        "/var/mobile/Containers/Data/Application/UUID/Documents/Notes", "omd-scratch"
    )
    assert got == "/var/mobile/Containers/Data/Application/UUID/Documents/omd-scratch"


def test_derive_sibling_vault_path_needs_an_open_vault():
    with pytest.raises(SystemExit):
        prov.derive_sibling_vault_path(None, "omd-scratch")


def test_derive_sibling_vault_path_refuses_external_vault():
    # An iCloud/external vault lives outside the app container, so the provisioned
    # /Documents/<vault> is not a sibling of it - --open must refuse, not reload empty.
    with pytest.raises(SystemExit) as exc:
        prov.derive_sibling_vault_path(
            "/private/var/mobile/Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes",
            "omd-scratch",
        )
    assert "not in the app's Documents container" in str(exc.value)


def test_derive_sibling_vault_path_refuses_non_documents_container_dir():
    # In-container but not directly under Documents: still not where AFC provisioned.
    with pytest.raises(SystemExit):
        prov.derive_sibling_vault_path(
            "/var/mobile/Containers/Data/Application/UUID/Library/Vaults/Notes", "omd-scratch"
        )


def test_open_hint_reflects_open_and_plugin():
    assert "reloading" in prov.open_hint(True, None, "android")
    assert "--open" in prov.open_hint(False, None, "ios")
    with_plugin = prov.open_hint(True, "metaedit", "android")
    assert "omd android reload --plugin metaedit" in with_plugin


# ---------- issue #4: plugin-namespaced default scratch-vault names ----------
def test_resolve_vault_name_explicit_wins():
    assert prov.resolve_vault_name("my-scratch", "quickadd") == ("my-scratch", "explicit")


def test_resolve_vault_name_derives_from_plugin():
    assert prov.resolve_vault_name(None, "quickadd") == ("quickadd-omd-scratch", "derived")


def test_resolve_vault_name_default_without_plugin():
    assert prov.resolve_vault_name(None, None) == ("omd-scratch", "default")


def test_resolve_vault_name_sanitizes_spaces_and_path_chars():
    name, source = prov.resolve_vault_name(None, "My Plugin!/..\\v2")
    assert source == "derived"
    assert name == "my-plugin-..-v2-omd-scratch"
    assert "/" not in name and "\\" not in name and " " not in name


def test_resolve_vault_name_is_stable_across_reruns():
    first = prov.resolve_vault_name(None, "Quick Add")
    assert first == prov.resolve_vault_name(None, "Quick Add")


def test_resolve_vault_name_unsanitizable_plugin_falls_back_to_default():
    assert prov.resolve_vault_name(None, "!!!") == ("omd-scratch", "default")


def test_derived_names_always_pass_the_scratch_guard():
    for plugin in ("quickadd", "My Plugin", "a/b", "UPPER_case.v2"):
        name, _ = prov.resolve_vault_name(None, plugin)
        prov.guard_provision_vault(name, confirm_real=False, test_vault=None)  # no raise
        prov.guard_remove_vault(name)  # no raise


def test_sanitize_vault_segment_strips_leading_trailing_dots_and_dashes():
    assert prov.sanitize_vault_segment("..hidden--") == "hidden"


# ---------- issue #5: storage-backed vault identity ----------
APP_PATH = "/var/mobile/Containers/Data/Application/ABC-123/Documents/omd-scratch"
ICLOUD_PATH = "/var/mobile/Library/Mobile Documents/iCloud~md~obsidian/Documents/omd-scratch"
EXTERNAL_PATH = "/private/var/mobile/Containers/Shared/AppGroup/XYZ/File Provider Storage/omd-scratch"


def test_classify_storage_app_container():
    assert prov.classify_storage(APP_PATH) == "app-container"


def test_classify_storage_icloud():
    assert prov.classify_storage(ICLOUD_PATH) == "icloud"
    assert prov.classify_storage("/x/com~apple~CloudDocs/omd-scratch") == "icloud"


def test_classify_storage_external():
    assert prov.classify_storage(EXTERNAL_PATH) == "external"


def test_classify_storage_unknown_when_no_selection():
    assert prov.classify_storage(None) == "unknown"
    assert prov.classify_storage("") == "unknown"


def test_vault_identity_shape():
    identity = prov.vault_identity("omd-scratch", ICLOUD_PATH)
    assert identity == {
        "vaultName": "omd-scratch",
        "selectedVaultPath": ICLOUD_PATH,
        "storageKind": "icloud",
    }


def test_describe_vault_identity_mentions_name_kind_and_path():
    text = prov.describe_vault_identity(prov.vault_identity("omd-scratch", ICLOUD_PATH))
    assert "'omd-scratch'" in text
    assert "icloud" in text
    assert ICLOUD_PATH in text


def test_describe_vault_identity_without_path():
    text = prov.describe_vault_identity(prov.vault_identity("notes", None))
    assert "unknown" in text
    assert "no recorded path" in text


def test_afc_vault_corresponds_same_name_different_storage_is_false():
    """The issue #5 scenario: two vaults both named omd-scratch."""
    assert prov.afc_vault_corresponds(APP_PATH, "omd-scratch") is True
    assert prov.afc_vault_corresponds(ICLOUD_PATH, "omd-scratch") is False
    assert prov.afc_vault_corresponds(EXTERNAL_PATH, "omd-scratch") is False


def test_afc_vault_corresponds_name_mismatch_is_false():
    assert prov.afc_vault_corresponds(APP_PATH, "other-vault") is False


def test_afc_vault_corresponds_no_selection_is_false():
    assert prov.afc_vault_corresponds(None, "omd-scratch") is False


def test_derive_sibling_error_names_storage_kind():
    import pytest as _pytest
    with _pytest.raises(SystemExit) as excinfo:
        prov.derive_sibling_vault_path(ICLOUD_PATH, "omd-scratch")
    assert "icloud-backed" in str(excinfo.value)


RELATIVE_APP_PATH = "documents/notes"


def test_classify_storage_relative_documents_is_app_container():
    assert prov.classify_storage(RELATIVE_APP_PATH) == "app-container"
    assert prov.classify_storage("Documents/notes") == "app-container"


def test_classify_storage_other_relative_paths_stay_external():
    assert prov.classify_storage("somewhere/notes") == "external"
    assert prov.classify_storage("documents") == "external"


def test_afc_vault_corresponds_relative_form():
    assert prov.afc_vault_corresponds(RELATIVE_APP_PATH, "notes") is True
    assert prov.afc_vault_corresponds(RELATIVE_APP_PATH, "other") is False


def test_derive_sibling_vault_path_relative_form_preserves_prefix():
    assert prov.derive_sibling_vault_path("documents/notes", "omd-scratch") == "documents/omd-scratch"
    assert prov.derive_sibling_vault_path("Documents/notes", "omd-scratch") == "Documents/omd-scratch"


def test_derive_sibling_vault_path_absolute_form_unchanged():
    got = prov.derive_sibling_vault_path(
        "/var/mobile/Containers/Data/Application/ABC/Documents/notes", "omd-scratch"
    )
    assert got == "/var/mobile/Containers/Data/Application/ABC/Documents/omd-scratch"
