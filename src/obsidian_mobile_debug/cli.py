"""``omd`` command-line entry point.

Two platform groups - ``omd ios <cmd>`` and ``omd android <cmd>`` - share a
common command surface (pages / eval / diagnose / reload / deploy / provision /
verify / logs). The
parser is pure argparse: it imports no transport, so ``omd --help``,
``omd ios --help`` and ``omd android --help`` run without a device connected or
pymobiledevice3/adb installed. The platform module (which touches the device) is
imported only when a command actually dispatches.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__

DEFAULT_BUNDLE = "md.obsidian"
DEFAULT_CDP_PORT = 9333


def _common_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--bundle", default=DEFAULT_BUNDLE,
                        help=f"app bundle id / android package (default: {DEFAULT_BUNDLE})")
    parent.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON where the command supports both forms")
    return parent


def _add_eval_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("expr", nargs="?", help="JavaScript expression (wrap statements in an async IIFE)")
    parser.add_argument("--probe", help="path to a .js probe file, or the name of a bundled probe (e.g. core_smoke)")
    parser.add_argument("--timeout", type=float, default=120.0, help="evaluation timeout in seconds")


def _add_deploy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plugin", required=True, help="plugin id to deploy")
    parser.add_argument("--repo", help="plugin repo path (derives main.js/manifest.json/styles.css)")
    parser.add_argument("--main", help="path to built main.js (overrides --repo)")
    parser.add_argument("--manifest", help="path to manifest.json (overrides --repo)")
    parser.add_argument("--styles", help="path to styles.css (optional)")
    parser.add_argument("--confirm-real-vault", action="store_true",
                        help="acknowledge this writes to a non-test vault")
    parser.add_argument("--test-vault", help="whitelist this exact vault name as a safe test vault")


def _add_provision_args(parser: argparse.ArgumentParser) -> None:
    from .provision import DEFAULT_VAULT

    parser.add_argument("--vault", default=None,
                        help=f"scratch vault name (default: <plugin>-{DEFAULT_VAULT} with "
                             f"--plugin, else {DEFAULT_VAULT})")
    parser.add_argument("--plugin", help="also deploy this plugin and enable it in the vault")
    parser.add_argument("--repo", help="plugin repo path (derives main.js/manifest.json/styles.css)")
    parser.add_argument("--main", help="path to built main.js (overrides --repo)")
    parser.add_argument("--manifest", help="path to manifest.json (overrides --repo)")
    parser.add_argument("--styles", help="path to styles.css (optional)")
    parser.add_argument("--data", help="seed the plugin's data.json from this file (first provision only)")
    parser.add_argument("--open", action="store_true",
                        help="after provisioning, switch Obsidian into the vault and reload (needs the app running)")
    parser.add_argument("--remove", action="store_true",
                        help="delete the scratch vault instead of provisioning it (scratch names only)")
    parser.add_argument("--confirm-real-vault", action="store_true",
                        help="acknowledge provisioning into a non-scratch-named vault (never unlocks --remove)")
    parser.add_argument("--test-vault", help="whitelist this exact vault name as a safe scratch vault")


def _add_verify_args(parser: argparse.ArgumentParser) -> None:
    from .provision import DEFAULT_VAULT

    parser.add_argument("--plugin", required=True, help="plugin id to verify")
    parser.add_argument("--repo", help="plugin repo path (derives main.js/manifest.json/styles.css)")
    parser.add_argument("--main", help="path to built main.js (overrides --repo)")
    parser.add_argument("--manifest", help="path to manifest.json (overrides --repo)")
    parser.add_argument("--styles", help="path to styles.css (optional)")
    parser.add_argument("--vault", default=None,
                        help=f"scratch vault name (default: <plugin>-{DEFAULT_VAULT})")
    parser.add_argument("--data", help="seed the plugin's data.json from this file (first provision only)")
    parser.add_argument("--probe", action="append",
                        help="probe to run (bundled name or .js path); repeatable; default: core_smoke")
    parser.add_argument("--probe-timeout", type=float, default=120.0,
                        help="per-probe evaluation timeout in seconds")
    parser.add_argument("--logs-seconds", type=int, default=0,
                        help="keep capturing console output this long after the probes")
    parser.add_argument("--keep-vault", action="store_true",
                        help="stay in the scratch vault afterwards (skip restoring the original)")
    parser.add_argument("--cleanup", action="store_true",
                        help="remove the scratch vault after restoring the original vault")
    parser.add_argument("--confirm-real-vault", action="store_true",
                        help="acknowledge verifying against a non-scratch-named vault")
    parser.add_argument("--test-vault", help="whitelist this exact vault name as a safe scratch vault")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omd",
        description=(
            "Debug Obsidian WebViews on iPhone, Android devices, emulators, and test containers."
        ),
    )
    from .provision import ANDROID_DEFAULT_ROOT

    parser.add_argument("--version", action="version", version=f"omd {__version__}")
    platforms = parser.add_subparsers(dest="platform", required=True)
    common = _common_parent()

    # ---------- iOS ----------
    ios = platforms.add_parser("ios", help="drive an iPhone WKWebView over USB (pymobiledevice3)")
    ios_sub = ios.add_subparsers(dest="cmd", required=True)

    ios_sub.add_parser("pages", parents=[common], help="list inspectable pages/webviews")

    p = ios_sub.add_parser("eval", parents=[common], help="evaluate JS against the page")
    _add_eval_args(p)

    p = ios_sub.add_parser("command", parents=[common], help="run an Obsidian command by id")
    p.add_argument("command_id")

    p = ios_sub.add_parser("diagnose", parents=[common], help="report runtime + (with --plugin) install state")
    p.add_argument("--plugin", help="also report this plugin's install/enable state")
    p.add_argument("--vault", help="vault folder name on device (auto-detected if single)")

    p = ios_sub.add_parser("reload", parents=[common], help="disable + enable a plugin")
    p.add_argument("--plugin", required=True)
    p.add_argument("--confirm-real-vault", action="store_true", help="allow reload against a non-test vault")
    p.add_argument("--test-vault", help="whitelist this exact vault name as a safe test vault")

    p = ios_sub.add_parser("deploy", parents=[common], help="AFC-push a built plugin, back up, then reload")
    _add_deploy_args(p)
    p.add_argument("--vault", help="vault folder name on device (auto-detected if single)")
    p.add_argument("--no-backup", action="store_true", help="skip the pre-deploy plugin-folder backup")

    p = ios_sub.add_parser("restore", parents=[common], help="restore a pre-deploy plugin backup")
    p.add_argument("--backup", help="backup dir (defaults to the newest below OMD_BACKUP_DIR)")
    p.add_argument("--force", action="store_true", help="allow restore when the backup device id differs")
    p.add_argument("--no-reload", action="store_true", help="copy files only; do not adjust enabled state")

    p = ios_sub.add_parser("provision", parents=[common],
                          help="create (or --remove) a scratch vault with an .obsidian skeleton")
    _add_provision_args(p)

    p = ios_sub.add_parser("verify", parents=[common],
                          help="run the full plugin verification loop in a scratch vault")
    _add_verify_args(p)

    p = ios_sub.add_parser("logs", parents=[common], help="stream console + uncaught errors")
    p.add_argument("--seconds", type=int, default=60)

    ios_sub.add_parser("backups", parents=[common], help="list local pre-deploy backups")

    # ---------- Android ----------
    android = platforms.add_parser("android", help="drive an Android WebView over adb + CDP")
    android_common = argparse.ArgumentParser(add_help=False)
    android_common.add_argument("--port", type=int, default=DEFAULT_CDP_PORT,
                                help=f"local TCP port for the CDP forward (default: {DEFAULT_CDP_PORT})")
    android_sub = android.add_subparsers(dest="cmd", required=True)

    p = android_sub.add_parser(
        "setup", parents=[common],
        help="install and boot a verified Android emulator + Obsidian runtime",
    )
    p.add_argument("--api", type=int, default=int(os.environ.get("API", "34")),
                   help="Android API level (default: 34)")
    p.add_argument(
        "--backend", choices=("emulator", "container"), default="emulator",
        help="Android runtime backend (default: emulator; container supports Linux VPS hosts)",
    )
    p.add_argument(
        "--abi", choices=("auto", "x86_64", "arm64-v8a"), default=os.environ.get("ABI", "auto"),
        help="Android system-image ABI (default: host-native; arm64-v8a avoids x86 TCG issues)",
    )
    p.add_argument("--avd", default=os.environ.get("AVD", "obsidian-debug"),
                   help="isolated AVD name (default: obsidian-debug)")
    p.add_argument("--device", default=os.environ.get("DEVICE", "pixel_6"),
                   help="avdmanager hardware profile (default: pixel_6)")
    p.add_argument(
        "--gpu", choices=("auto", "software", "swiftshader", "lavapipe", "host"), default="auto",
        help="emulator graphics backend (default: software without acceleration, auto otherwise)",
    )
    p.add_argument("--sdk-root", default=os.environ.get("ANDROID_SDK_ROOT"),
                   help="Android SDK root (default: isolated OMD cache)")
    p.add_argument("--android-home", default=os.environ.get("ANDROID_USER_HOME"),
                   help="isolated Android user/AVD state directory")
    p.add_argument("--state-dir", default=os.environ.get("OMD_ANDROID_STATE_DIR"),
                   help="downloads, SDK, logs, and runtime state directory")
    p.add_argument("--apk", default=os.environ.get("OBSIDIAN_APK"),
                   help="local Obsidian APK (signature is still verified)")
    p.add_argument("--acceleration", choices=("auto", "on", "off"), default="auto",
                   help="hardware acceleration policy (default: auto; off supports no-KVM hosts)")
    p.add_argument("--adb-timeout", type=float, default=1200,
                   help="seconds to wait for the initial ADB connection (default: 1200)")
    p.add_argument("--boot-timeout", type=float, default=2400,
                   help="seconds to wait for a stable Android boot (default: 2400)")
    p.add_argument("--console-port", type=int, default=5554,
                   help="emulator console port, an even number from 5554-5682 (default: 5554)")
    p.add_argument("--adb-port", type=int, default=5555,
                   help="loopback ADB port for the container backend (default: 5555)")
    p.add_argument("--reset", action="store_true",
                   help="replace and wipe the named AVD or OMD container data before booting")
    p.add_argument("--timeout-multiplier", type=int, default=50,
                   help="Android service timeout multiplier for software emulation (default: 50)")
    p.add_argument(
        "--signer-sha256",
        default=os.environ.get(
            "OBSIDIAN_SIGNER_SHA256",
            "bd3bd52f4427b5ab7f8059d071a35e3de943c646546d32313da5f1cac254b7e4",
        ),
        help="required Obsidian APK signer certificate SHA-256",
    )
    p.add_argument(
        "--acknowledge-privileged-container", action="store_true",
        help="acknowledge that ReDroid is a privileged, test-only container without SELinux",
    )

    android_sub.add_parser("pages", parents=[common, android_common], help="list CDP targets")

    p = android_sub.add_parser("eval", parents=[common, android_common], help="evaluate JS against the page")
    _add_eval_args(p)
    p.add_argument("--no-await", action="store_true", help="do not awaitPromise on the result")

    p = android_sub.add_parser("diagnose", parents=[common, android_common],
                              help="report runtime + (with --plugin) plugin state")
    p.add_argument("--plugin", help="also report this plugin's install/enable state")

    p = android_sub.add_parser("reload", parents=[common, android_common], help="disable + enable a plugin")
    p.add_argument("--plugin", required=True)
    p.add_argument("--confirm-real-vault", action="store_true", help="allow reload against a non-test vault")
    p.add_argument("--test-vault", help="whitelist this exact vault name as a safe test vault")

    p = android_sub.add_parser("deploy", parents=[common, android_common],
                              help="adb-push a built plugin to a scratch vault, then reload")
    _add_deploy_args(p)
    p.add_argument("--vault-path", required=True, help="absolute on-device vault path (e.g. /sdcard/Documents/Scratch)")

    p = android_sub.add_parser("provision", parents=[common, android_common],
                              help="create (or --remove) a scratch vault with an .obsidian skeleton")
    _add_provision_args(p)
    p.add_argument("--vault-root", default=ANDROID_DEFAULT_ROOT,
                   help=f"on-device parent dir for the scratch vault (default: {ANDROID_DEFAULT_ROOT})")

    p = android_sub.add_parser("verify", parents=[common, android_common],
                              help="run the full plugin verification loop in a scratch vault")
    _add_verify_args(p)
    p.add_argument("--vault-root", default=ANDROID_DEFAULT_ROOT,
                   help=f"on-device parent dir for the scratch vault (default: {ANDROID_DEFAULT_ROOT})")

    p = android_sub.add_parser("logs", parents=[common, android_common], help="stream logcat (Obsidian/WebView/crash)")
    p.add_argument("--seconds", type=int, default=60)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.platform == "ios":
        from . import ios
        return ios.dispatch(args)
    if args.platform == "android":
        from . import android
        return android.dispatch(args)
    return 1  # unreachable: subparsers are required


if __name__ == "__main__":
    sys.exit(main())
