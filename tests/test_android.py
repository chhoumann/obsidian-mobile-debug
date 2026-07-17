"""Android pure helpers: adb command construction, path/vault logic, guards."""
import argparse
import json

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
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(android.Path, "is_file", lambda _self: False)
    assert android.adb_bin() == "adb"


def test_adb_bin_uses_sdk_root(monkeypatch, tmp_path):
    adb = tmp_path / "platform-tools/adb"
    adb.parent.mkdir()
    adb.touch()
    monkeypatch.delenv("ADB", raising=False)
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path))
    assert android.adb_bin() == str(adb)


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


# ---------- provision ----------
def test_android_vault_dir_joins_and_normalizes():
    assert android.android_vault_dir("/storage/emulated/0/Documents", "omd-scratch") == \
        "/storage/emulated/0/Documents/omd-scratch"
    assert android.android_vault_dir("/sdcard/Documents/", "omd-scratch") == \
        "/sdcard/Documents/omd-scratch"


def test_existing_vault_files_parses_find_output(monkeypatch):
    vault = "/sdcard/Documents/omd-scratch"
    listing = (
        f"{vault}/.obsidian/app.json\n"
        f"{vault}/.obsidian/community-plugins.json\n"
        f"{vault}/.obsidian/plugins/metaedit/data.json\n"
    )
    monkeypatch.setattr(android, "adb_out", lambda args, *, check=True: listing)
    assert android.existing_vault_files(vault) == {
        ".obsidian/app.json",
        ".obsidian/community-plugins.json",
        ".obsidian/plugins/metaedit/data.json",
    }


def test_existing_vault_files_empty_when_vault_absent(monkeypatch):
    # `find` on a missing dir prints nothing (and exits non-zero, tolerated).
    monkeypatch.setattr(android, "adb_out", lambda args, *, check=True: "")
    assert android.existing_vault_files("/sdcard/Documents/omd-scratch") == set()


class _FakeAdb:
    """Records adb invocations so provision routing can be asserted without a device."""

    def __init__(self, *, existing_listing="", exists=True):
        self.existing_listing = existing_listing
        self.exists = exists
        self.pushes: list[tuple[str, str]] = []
        self.shell_calls: list[list[str]] = []

    def adb_out(self, args, *, check=True):
        if args[:2] == ["shell", "find"]:
            return self.existing_listing
        if args[:2] == ["shell", "ls"]:
            return "present" if self.exists else ""
        if args[:2] == ["shell", "mkdir"]:
            self.shell_calls.append(args)
            return ""
        return ""

    def run_adb(self, args, *, check=True):
        if args and args[0] == "push":
            self.pushes.append((args[1], args[2]))
        else:
            self.shell_calls.append(args)
        return None


def _run_provision(monkeypatch, args, fake):
    import asyncio

    monkeypatch.setattr(android, "adb_out", fake.adb_out)
    monkeypatch.setattr(android, "run_adb", fake.run_adb)
    return asyncio.run(android.cmd_provision(args))


def test_dispatch_runs_setup_outside_asyncio(monkeypatch):
    from obsidian_mobile_debug import android_setup

    args = argparse.Namespace(cmd="setup")
    monkeypatch.setattr(android_setup, "setup_from_args", lambda received: 7)

    assert android.dispatch(args) == 7


def test_cmd_provision_first_run_writes_full_skeleton(monkeypatch, capsys):
    fake = _FakeAdb(existing_listing="")
    args = argparse.Namespace(
        vault="omd-scratch", vault_root="/sdcard/Documents", plugin=None, repo=None,
        main=None, manifest=None, styles=None, data=None, remove=False, open=False,
        confirm_real_vault=False, test_vault=None,
    )
    assert _run_provision(monkeypatch, args, fake) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["action"] == "provision"
    assert set(report["wrote"]) == {
        ".obsidian/app.json", ".obsidian/appearance.json", ".obsidian/core-plugins.json",
        ".obsidian/community-plugins.json", ".obsidian/workspace.json",
    }
    # Every skeleton file was pushed to the right absolute path.
    pushed_targets = {dest for _src, dest in fake.pushes}
    assert "/sdcard/Documents/omd-scratch/.obsidian/app.json" in pushed_targets


def test_cmd_provision_idempotent_rerun_skips_and_rewrites(monkeypatch, capsys):
    vault = "/sdcard/Documents/omd-scratch"
    listing = "\n".join(
        f"{vault}/.obsidian/{name}"
        for name in ("app.json", "appearance.json", "core-plugins.json",
                     "community-plugins.json", "workspace.json")
    )
    fake = _FakeAdb(existing_listing=listing)
    args = argparse.Namespace(
        vault="omd-scratch", vault_root="/sdcard/Documents", plugin=None, repo=None,
        main=None, manifest=None, styles=None, data=None, remove=False, open=False,
        confirm_real_vault=False, test_vault=None,
    )
    assert _run_provision(monkeypatch, args, fake) == 0
    report = json.loads(capsys.readouterr().out)
    # Only community-plugins.json (overwrite=True) is rewritten; the rest are skipped.
    assert report["wrote"] == [".obsidian/community-plugins.json"]
    assert ".obsidian/app.json" in report["skipped"]


def test_cmd_provision_remove_guards_and_deletes(monkeypatch, capsys):
    fake = _FakeAdb(exists=True)
    args = argparse.Namespace(
        vault="omd-scratch", vault_root="/sdcard/Documents", remove=True,
    )
    assert _run_provision(monkeypatch, args, fake) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["action"] == "remove" and report["removed"] is True
    assert any(call[:3] == ["shell", "rm", "-rf"] for call in fake.shell_calls)


def test_cmd_provision_remove_refuses_real_vault(monkeypatch):
    fake = _FakeAdb(exists=True)
    args = argparse.Namespace(vault="my-notes", vault_root="/sdcard/Documents", remove=True)
    with pytest.raises(SystemExit):
        _run_provision(monkeypatch, args, fake)


def test_cmd_provision_open_switches_vault_over_cdp(monkeypatch, capsys):
    import contextlib

    fake = _FakeAdb(existing_listing="")
    evaled: list[str] = []

    @contextlib.contextmanager
    def fake_forward(port, package):
        yield 4321

    async def fake_ev(port, expr, **kwargs):
        evaled.append(expr)
        return {"opened": "/sdcard/Documents/omd-scratch"}

    monkeypatch.setattr(android, "cdp_forward", fake_forward)
    monkeypatch.setattr(android, "ev", fake_ev)
    args = argparse.Namespace(
        vault="omd-scratch", vault_root="/sdcard/Documents", plugin=None, repo=None,
        main=None, manifest=None, styles=None, data=None, remove=False, open=True,
        confirm_real_vault=False, test_vault=None, port=9333, bundle="md.obsidian",
    )
    assert _run_provision(monkeypatch, args, fake) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["opened"] == {"opened": "/sdcard/Documents/omd-scratch"}
    # The open eval targets the provisioned vault path via localStorage + reload.
    assert any("mobile-selected-vault" in expr and "location.reload()" in expr for expr in evaled)


def test_cmd_provision_refuses_real_vault_without_confirm(monkeypatch):
    fake = _FakeAdb(existing_listing="")
    args = argparse.Namespace(
        vault="my-notes", vault_root="/sdcard/Documents", plugin=None, repo=None,
        main=None, manifest=None, styles=None, data=None, remove=False, open=False,
        confirm_real_vault=False, test_vault=None,
    )
    with pytest.raises(SystemExit):
        _run_provision(monkeypatch, args, fake)
