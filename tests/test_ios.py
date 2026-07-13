"""iOS pure helpers: backup path derivation, vault guards, file resolution."""
import argparse
from pathlib import Path

import pytest

from obsidian_mobile_debug import ios


def test_backup_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("OMD_BACKUP_DIR", str(tmp_path / "bk"))
    assert ios.backup_root() == tmp_path / "bk"


def test_backup_root_default(monkeypatch):
    monkeypatch.delenv("OMD_BACKUP_DIR", raising=False)
    assert ios.backup_root() == Path.home() / ".obsidian-mobile-debug" / "backups"


def test_backup_dir_for_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("OMD_BACKUP_DIR", str(tmp_path))
    got = ios.backup_dir_for("udid-123", "scratch", "dataview", "20260101T000000Z")
    assert got == tmp_path / "udid-123" / "scratch" / "dataview" / "20260101T000000Z"


def test_backup_dir_for_sanitizes_segments(monkeypatch, tmp_path):
    monkeypatch.setenv("OMD_BACKUP_DIR", str(tmp_path))
    got = ios.backup_dir_for("dev/../id", "va ult", "plug in", "stamp")
    assert got == tmp_path / "dev-..-id" / "va-ult" / "plug-in" / "stamp"


def test_plugin_dir_for():
    assert ios.plugin_dir_for("/Documents/notes", "dataview") == "/Documents/notes/.obsidian/plugins/dataview"


def test_looks_like_test_vault():
    assert ios.looks_like_test_vault("ScratchVault")
    assert not ios.looks_like_test_vault("notes")
    assert ios.looks_like_test_vault("notes", expected="notes")


def test_guard_real_vault_blocks_real():
    args = argparse.Namespace(confirm_real_vault=False, test_vault=None)
    with pytest.raises(SystemExit):
        ios.guard_real_vault("notes", args, "deploy")


def test_guard_real_vault_allows_flag():
    args = argparse.Namespace(confirm_real_vault=True, test_vault=None)
    ios.guard_real_vault("notes", args, "deploy")  # no raise


def test_resolve_plugin_files_from_repo(tmp_path):
    (tmp_path / "main.js").write_text("//", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    args = argparse.Namespace(main=None, manifest=None, styles=None, repo=str(tmp_path))
    files = ios.resolve_plugin_files(args)
    assert set(files) == {"main.js", "manifest.json"}


def test_resolve_plugin_files_includes_styles(tmp_path):
    (tmp_path / "main.js").write_text("//", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "styles.css").write_text("/* */", encoding="utf-8")
    args = argparse.Namespace(main=None, manifest=None, styles=None, repo=str(tmp_path))
    assert "styles.css" in ios.resolve_plugin_files(args)


def test_resolve_plugin_files_from_build_subdir(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "main.js").write_text("//", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    args = argparse.Namespace(main=None, manifest=None, styles=None, repo=str(tmp_path))
    files = ios.resolve_plugin_files(args)
    assert files["main.js"].endswith("build/main.js")


def test_resolve_plugin_files_needs_repo_or_main():
    args = argparse.Namespace(main=None, manifest=None, styles=None, repo=None)
    with pytest.raises(SystemExit):
        ios.resolve_plugin_files(args)


class _FakeApp:
    def __init__(self, bundle, name):
        self.bundle = bundle
        self.name = name


class _FakePage:
    def __init__(self, bundle, name):
        self.application = _FakeApp(bundle, name)


def test_page_matches_bundle_exact():
    page = _FakePage("com.example.app", "Example")
    assert ios.page_matches_bundle(page, "com.example.app")


def test_page_matches_bundle_default_name_fallback():
    # Obsidian's page may not report bundle == md.obsidian; name fallback applies
    # only for the default bundle.
    page = _FakePage("com.apple.WebKit.WebContent", "Obsidian")
    assert ios.page_matches_bundle(page, ios.DEFAULT_BUNDLE)


def test_page_matches_bundle_non_default_is_strict():
    # An explicit --bundle for another app must not silently match Obsidian.
    page = _FakePage("com.apple.WebKit.WebContent", "Obsidian")
    assert not ios.page_matches_bundle(page, "com.example.app")


def test_safe_segment():
    assert ios.safe_segment("a/b c") == "a-b-c"
    assert ios.safe_segment("///") == "unknown"


def test_guard_real_vault_includes_identity_when_available():
    from obsidian_mobile_debug import provision as prov

    args = argparse.Namespace(confirm_real_vault=False, test_vault=None)
    identity = prov.vault_identity(
        "notes", "/var/mobile/Library/Mobile Documents/iCloud~md~obsidian/Documents/notes"
    )
    with pytest.raises(SystemExit) as excinfo:
        ios.guard_real_vault("notes", args, "deploy", identity)
    message = str(excinfo.value)
    assert "Vault identity:" in message
    assert "icloud" in message
    assert "Mobile Documents" in message
