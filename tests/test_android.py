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


def _stub_adb_out(monkeypatch, *, pidof: str, unix_table: str):
    def fake_adb_out(args, *, check=True):
        if args[:2] == ["shell", "pidof"]:
            return pidof
        if args[:2] == ["shell", "cat"]:
            return unix_table
        raise AssertionError(f"unexpected adb_out call: {args}")

    monkeypatch.setattr(android, "adb_out", fake_adb_out)


def test_discover_socket_matches_pid_from_unix_table(monkeypatch):
    table = "Num ...\n0000: 00000002 @webview_devtools_remote_3632\n"
    _stub_adb_out(monkeypatch, pidof="3632", unix_table=table)
    assert android.discover_socket("md.obsidian") == ("webview_devtools_remote_3632", 3632)


def test_discover_socket_falls_back_when_proc_net_unix_blocked(monkeypatch):
    # Restricted /proc/net/unix: `cat` exits non-zero and adb_out(check=False)
    # returns an empty table. Discovery must still construct the socket from pidof.
    _stub_adb_out(monkeypatch, pidof="4321", unix_table="")
    assert android.discover_socket("md.obsidian") == ("webview_devtools_remote_4321", 4321)


def test_discover_socket_not_running_raises(monkeypatch):
    _stub_adb_out(monkeypatch, pidof="", unix_table="")
    with pytest.raises(SystemExit):
        android.discover_socket("md.obsidian")
