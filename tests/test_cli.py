"""Argument parsing - the parser must build and route without any device."""
import pytest

from obsidian_mobile_debug.cli import DEFAULT_BUNDLE, DEFAULT_CDP_PORT, build_parser


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_ios_eval_defaults():
    args = parse("ios", "eval", "app.vault.getName()")
    assert args.platform == "ios"
    assert args.cmd == "eval"
    assert args.expr == "app.vault.getName()"
    assert args.bundle == DEFAULT_BUNDLE
    assert args.probe is None
    assert args.json is False


def test_ios_eval_with_probe():
    args = parse("ios", "eval", "--probe", "core_smoke")
    assert args.expr is None
    assert args.probe == "core_smoke"


def test_ios_deploy_requires_plugin():
    with pytest.raises(SystemExit):
        parse("ios", "deploy", "--repo", "/tmp/plug")  # missing --plugin


def test_ios_deploy_flags():
    args = parse("ios", "deploy", "--plugin", "dataview", "--repo", "/tmp/dv",
                 "--vault", "scratch", "--confirm-real-vault")
    assert args.plugin == "dataview"
    assert args.repo == "/tmp/dv"
    assert args.vault == "scratch"
    assert args.confirm_real_vault is True
    assert args.no_backup is False


def test_android_defaults_and_port():
    args = parse("android", "eval", "1+1")
    assert args.platform == "android"
    assert args.port == DEFAULT_CDP_PORT
    assert args.no_await is False
    assert args.bundle == DEFAULT_BUNDLE


def test_android_deploy_requires_vault_path():
    with pytest.raises(SystemExit):
        parse("android", "deploy", "--plugin", "dataview", "--repo", "/tmp/dv")


def test_android_custom_port():
    args = parse("android", "eval", "--port", "9444", "1+1")
    assert args.port == 9444


def test_platform_required():
    with pytest.raises(SystemExit):
        parse()


def test_bundle_override():
    args = parse("ios", "pages", "--bundle", "com.example.app")
    assert args.bundle == "com.example.app"


def test_ios_provision_defaults():
    args = parse("ios", "provision")
    assert args.cmd == "provision"
    assert args.vault == "omd-scratch"
    assert args.plugin is None
    assert args.remove is False
    assert args.confirm_real_vault is False


def test_ios_provision_with_plugin_and_data():
    args = parse("ios", "provision", "--plugin", "metaedit", "--repo", "/tmp/me",
                 "--data", "/tmp/seed.json")
    assert args.plugin == "metaedit"
    assert args.repo == "/tmp/me"
    assert args.data == "/tmp/seed.json"


def test_android_provision_defaults_root():
    args = parse("android", "provision")
    assert args.vault == "omd-scratch"
    assert args.vault_root == "/storage/emulated/0/Documents"
    assert args.port == DEFAULT_CDP_PORT


def test_android_provision_remove():
    args = parse("android", "provision", "--remove", "--vault", "omd-scratch")
    assert args.remove is True
    assert args.vault == "omd-scratch"
