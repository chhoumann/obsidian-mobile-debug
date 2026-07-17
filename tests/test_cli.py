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


def test_android_setup_defaults():
    args = parse("android", "setup")
    assert args.backend == "emulator"
    assert args.api == 34
    assert args.abi == "auto"
    assert args.avd == "obsidian-debug"
    assert args.device == "pixel_6"
    assert args.gpu == "auto"
    assert args.acceleration == "auto"
    assert args.adb_timeout == 1200
    assert args.boot_timeout == 2400
    assert args.console_port == 5554
    assert args.adb_port == 5555
    assert args.timeout_multiplier == 50
    assert args.reset is False
    assert args.acknowledge_privileged_container is False


def test_android_setup_accepts_software_emulator_options():
    args = parse(
        "android", "setup", "--acceleration", "off", "--avd", "vps-debug",
        "--abi", "arm64-v8a", "--console-port", "5556", "--reset",
    )
    assert args.acceleration == "off"
    assert args.avd == "vps-debug"
    assert args.abi == "arm64-v8a"
    assert args.console_port == 5556
    assert args.reset is True


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
    # None means "derive": <plugin>-omd-scratch with --plugin, else omd-scratch.
    assert args.vault is None
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
    assert args.vault is None
    assert args.vault_root == "/storage/emulated/0/Documents"
    assert args.port == DEFAULT_CDP_PORT


def test_android_provision_remove():
    args = parse("android", "provision", "--remove", "--vault", "omd-scratch")
    assert args.remove is True
    assert args.vault == "omd-scratch"


def test_provision_open_flag_defaults_off():
    assert parse("android", "provision").open is False
    assert parse("ios", "provision", "--open").open is True
