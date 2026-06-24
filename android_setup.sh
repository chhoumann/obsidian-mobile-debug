#!/usr/bin/env bash
# android_setup.sh — stand up an Android Obsidian WebView debugging environment
# from scratch: Android SDK + emulator + Obsidian + a CDP endpoint. Generic and
# re-runnable; everything it installs is removable with android_teardown.sh.
#
# Usage:   ./android_setup.sh
# Config (env overrides):
#   API=34  AVD=obsidian-debug  DEVICE=pixel_6  BUNDLE=md.obsidian  CDP_PORT=9333
#   OBSIDIAN_APK=/path/to.apk   # skip the GitHub download and use a local APK
#
# After it finishes, attach with:
#   uv run --no-project --with websockets python android_cdp.py eval 'app.vault.getName()'
set -euo pipefail

API="${API:-34}"
AVD="${AVD:-obsidian-debug}"
DEVICE="${DEVICE:-pixel_6}"
BUNDLE="${BUNDLE:-md.obsidian}"
CDP_PORT="${CDP_PORT:-9333}"
case "$(uname -m)" in arm64|aarch64) ABI=arm64-v8a ;; *) ABI=x86_64 ;; esac
IMAGE="system-images;android-${API};google_apis;${ABI}"

export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/homebrew/share/android-commandlinetools}"
export ANDROID_HOME="$ANDROID_SDK_ROOT"
ADB="$ANDROID_SDK_ROOT/platform-tools/adb"
EMU="$ANDROID_SDK_ROOT/emulator/emulator"

say(){ printf '\n=== %s ===\n' "$*"; }

say "1/5 tools (Homebrew + JDK + cmdline-tools)"
command -v brew >/dev/null || { echo "Homebrew required: https://brew.sh"; exit 1; }
java -version >/dev/null 2>&1 || brew install --cask temurin
command -v sdkmanager >/dev/null 2>&1 || brew install --cask android-commandlinetools
yes | sdkmanager --licenses >/dev/null 2>&1 || true
sdkmanager "platform-tools" "emulator" "platforms;android-${API}" "$IMAGE" >/dev/null

say "2/5 AVD '$AVD' ($DEVICE, $ABI)"
"$EMU" -list-avds 2>/dev/null | grep -qx "$AVD" \
  || echo no | avdmanager create avd -n "$AVD" -d "$DEVICE" -k "$IMAGE" --force

say "3/5 boot emulator (headless)"
if ! "$ADB" devices | grep -q emulator; then
  "$EMU" -avd "$AVD" -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect -no-snapshot \
    >"/tmp/${AVD}-emulator.log" 2>&1 &
fi
"$ADB" wait-for-device
until [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 2; done
"$ADB" root >/dev/null 2>&1 || true
"$ADB" wait-for-device
echo "booted: $("$ADB" shell getprop ro.build.version.release | tr -d '\r') ($ABI)"

say "4/5 install + launch Obsidian"
APK="${OBSIDIAN_APK:-}"
if [ -z "$APK" ]; then
  url=$(curl -s "https://api.github.com/repos/obsidianmd/obsidian-releases/releases" \
        | python3 -c "import sys,json;rs=json.load(sys.stdin);print(next(a['browser_download_url'] for r in rs for a in r.get('assets',[]) if a['name'].endswith('.apk')))")
  APK="/tmp/$(basename "$url")"
  [ -f "$APK" ] || { echo "downloading $url"; curl -sL -o "$APK" "$url"; }
fi
"$ADB" shell pm list packages 2>/dev/null | grep -q "$BUNDLE" || "$ADB" install -r "$APK" >/dev/null
"$ADB" shell am start -n "$BUNDLE/.MainActivity" >/dev/null
sleep 6

say "5/5 expose CDP on :$CDP_PORT"
PID=$("$ADB" shell pidof "$BUNDLE" | tr -d '\r')
SOCK=$("$ADB" shell cat /proc/net/unix 2>/dev/null | tr -d '\r' | grep -o "webview_devtools_remote_${PID}" | head -1)
[ -n "$SOCK" ] || { echo "no WebView devtools socket for pid $PID — is the app foregrounded & debuggable?"; exit 1; }
"$ADB" forward tcp:"$CDP_PORT" localabstract:"$SOCK" >/dev/null
echo "CDP ready -> http://localhost:$CDP_PORT/json"

cat <<EOF

Done. The emulator is on Obsidian's first-run screen. From here (CDP via android_cdp.py):
  P="uv run --no-project --with websockets python android_cdp.py"

  # 1. Create/open a vault by driving the onboarding DOM, e.g.:
  \$P eval '[...document.querySelectorAll("button")].map(b=>b.innerText.trim())'   # see buttons, click via .click()
  #    (onboarding markup changes between versions — inspect & click the right buttons)

  # 2. Community plugins are blocked by Restricted mode — turn it off:
  \$P eval '(async()=>{await app.plugins.setEnable(true);return app.plugins.isEnabled()})()'

  # 3. Deploy a plugin build (vault is App-storage by default):
  V=/sdcard/Android/data/$BUNDLE/files/<vault>/.obsidian/plugins/<id>
  $ADB shell mkdir -p "\$V"
  $ADB push build/main.js manifest.json "\$V"/
  \$P eval '(async()=>{await app.plugins.loadManifests();await app.plugins.enablePlugin("<id>");return !!app.plugins.plugins["<id>"]})()'

Tear it all down (and reclaim disk) with:  REMOVE_SDK=1 AVD=$AVD ./android_teardown.sh
EOF
