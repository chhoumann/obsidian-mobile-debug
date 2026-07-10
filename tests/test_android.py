"""Android pure helpers: adb command construction, path/vault logic, guards."""
import argparse

import pytest

from obsidian_mobile_debug import android


def test_forward_command():
    assert android.forward_command(9333, "webview_devtools_remote_1234") == [
        "forward", "tcp:9333", "localabstract:webview_devtools_remote_1234",
    ]


def test_forward_command_custom_port():
    assert android.forward_command(9444, "webview_devtools_remote_9") == [
        "forward", "tcp:9444", "localabstract:webview_devtools_remote_9",
    ]


def test_forward_remove_command():
    assert android.forward_remove_command(9333) == ["forward", "--remove", "tcp:9333"]


def test_adb_bin_env_override(monkeypatch):
    monkeypatch.setenv("ADB", "/opt/tools/adb")
    assert android.adb_bin() == "/opt/tools/adb"


def test_adb_bin_default(monkeypatch):
    monkeypatch.delenv("ADB", raising=False)
    assert android.adb_bin() == "adb"


def test_normalize_android_path_ok():
    assert android.normalize_android_path("/sdcard/Documents/Scratch/") == "/sdcard/Documents/Scratch"


def test_normalize_android_path_relative_rejected():
    with pytest.raises(SystemExit):
        android.normalize_android_path("Documents/Scratch")


def test_looks_like_test_vault():
    assert android.looks_like_test_vault("MyScratchVault")
    assert android.looks_like_test_vault("debug-vault")
    assert not android.looks_like_test_vault("notes")
    assert android.looks_like_test_vault("notes", expected="notes")


def test_guard_real_vault_blocks_real(monkeypatch):
    args = argparse.Namespace(confirm_real_vault=False, test_vault=None)
    with pytest.raises(SystemExit):
        android.guard_real_vault("notes", args, "deploy")


def test_guard_real_vault_allows_flag():
    args = argparse.Namespace(confirm_real_vault=True, test_vault=None)
    android.guard_real_vault("notes", args, "deploy")  # no raise


def test_guard_real_vault_allows_test_name():
    args = argparse.Namespace(confirm_real_vault=False, test_vault=None)
    android.guard_real_vault("scratch-vault", args, "deploy")  # no raise
