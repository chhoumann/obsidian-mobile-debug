#!/usr/bin/env bash
# android_teardown.sh — kill the emulator and delete the AVD. With REMOVE_SDK=1,
# also remove the Android SDK + cask + ~/.android to reclaim disk.
#
# Usage:
#   ./android_teardown.sh                 # kill emulator + delete the AVD
#   REMOVE_SDK=1 ./android_teardown.sh    # also uninstall the SDK (full clean)
# Config: AVD=obsidian-debug
set -uo pipefail

AVD="${AVD:-obsidian-debug}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/homebrew/share/android-commandlinetools}"
ADB="$ANDROID_SDK_ROOT/platform-tools/adb"

echo "=== kill emulator ==="
"$ADB" emu kill 2>/dev/null || true
sleep 3

echo "=== delete AVD '$AVD' ==="
avdmanager delete avd -n "$AVD" 2>/dev/null || true

if [ "${REMOVE_SDK:-0}" = "1" ]; then
  echo "=== remove SDK + cask + ~/.android ==="
  brew uninstall --cask android-commandlinetools 2>/dev/null || true
  rm -rf "$ANDROID_SDK_ROOT" "$HOME/.android"
  rm -f /tmp/Obsidian-*.apk /tmp/*-emulator.log 2>/dev/null || true
  echo "SDK fully removed. Re-create everything with ./android_setup.sh"
else
  echo "AVD deleted + emulator killed. SDK kept (REMOVE_SDK=1 to remove it too)."
fi
