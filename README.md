# obsidian-mobile-debug

Tooling + hard-won notes for **debugging Obsidian (and other WKWebView apps) on a
physical iPhone over USB** — driving the page's JS, deploying a locally-built
plugin, reproducing user-reported bugs, and capturing crashes — all from the Mac
terminal.

Built while fixing a real PodNotes bug: downloading a large podcast episode
OOM-killed Obsidian on mobile. This kit is what made it possible to reproduce it
on-device, fix it, and verify the fix end-to-end. Kept private for reuse.

## How it works

Obsidian on iOS is a Capacitor **WKWebView** app. Two USB channels, both via
[`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3), do everything:

- **WebKit Web Inspector** (`com.apple.webinspector`, over usbmux/lockdown) — open
  an `InspectorSession` against the page and evaluate arbitrary JS against
  Obsidian's `app` API. This is how we read state, enable/reload plugins, and
  trigger actions.
- **AFC / house_arrest** (`com.apple.mobile.house_arrest`, documents mode) — read
  and write files inside the app's container (the vault lives at
  `/Documents/<vault>/`). This is how we push a built plugin onto the phone.

There is **no Chrome-style `--remote-debugging-port`** on Safari/Obsidian; the
inspector protocol is reached over Apple's own transport, which is why
`pymobiledevice3` (not `ios-webkit-debug-proxy`) is the tool — the latter doesn't
support iOS 17+.

## Prerequisites

- macOS, iPhone connected via USB and unlocked/trusted.
- On the phone: **Settings → Apps → Safari → Advanced → Web Inspector = ON**
  (iOS 18+ path). The target app's WebView must be `isInspectable` — Obsidian is.
- Install pymobiledevice3 (uv preferred):

  ```bash
  uv tool install pymobiledevice3        # binary -> ~/.local/bin/pymobiledevice3
  ```

  On **iOS 17+ the webinspector + AFC services work directly over usbmux — no
  sudo RemoteXPC tunnel needed** (unlike most other developer services).

- Run the scripts with the uv-tool interpreter (it has pymobiledevice3):

  ```bash
  ~/.local/share/uv/tools/pymobiledevice3/bin/python podnotes_ios.py <cmd>
  # or, reproducibly, without a global install:
  uv run --no-project --with pymobiledevice3 python podnotes_ios.py <cmd>
  ```

## Scripts

### `obsidian_mobile_debug.py` — generalized CLI (start here)
Config-driven; nothing hardcoded. Pass `--bundle` (default `md.obsidian`),
`--plugin`, `--repo` as needed.

| Command | What it does |
|---|---|
| `pages` | List inspectable pages/webviews on the device. |
| `eval "<js>"` | Evaluate an async-aware JS expression against the page; print JSON. |
| `command <id>` | Run an Obsidian command by id (`executeCommandById`). |
| `diagnose --plugin <id>` | Report a plugin's install/enable state. |
| `deploy --plugin <id> --repo <path>` | AFC-push the build (`main.js` + `manifest.json`, plus `styles.css` if present) into the vault, then reload (disable → enable). Override paths with `--main/--manifest/--styles`; pass `--vault <name>` if you have more than one vault. |
| `reload --plugin <id>` | disable → enable the plugin. |
| `logs [--seconds N]` | Stream console + uncaught errors. |

```bash
P=~/.local/share/uv/tools/pymobiledevice3/bin/python
$P obsidian_mobile_debug.py pages
$P obsidian_mobile_debug.py eval 'app.vault.getName()'
$P obsidian_mobile_debug.py deploy --plugin dataview --repo ~/Developer/dataview
$P obsidian_mobile_debug.py reload --plugin dataview
$P obsidian_mobile_debug.py command app:reload
```

`deploy` derives the build files from `--repo` (`main.js` at the repo root or
`build/main.js`; `manifest.json`; optional `styles.css`); override any with the
explicit flags. Works against any WKWebView app via `--bundle`.

### `podnotes_ios.py` — worked example (PodNotes-specific)
The original, with constants hardcoded at the top (`BUNDLE`, `PLUGIN_ID`,
`REPO`) and two extra **download-specific** commands used to chase the OOM bug:
`repro [--count N]` (stack concurrent downloads to force the crash) and `verify`
(fire one download and watch the on-disk file grow chunk-by-chunk). Kept as a
reference for how to write plugin-specific reproduction commands; for everything
else use the generalized CLI above.

### `obsidian_inspect_demo.py` — read-only intro
Connects and reads vault state (name, file counts, plugins, dark mode, recent
notes). Good first contact / smoke test.

### `afc_discover.py` — find the vault path
Lists the app's `/Documents` container over AFC and locates the vault dir that
contains `.obsidian` (and the target plugin folder). Use when the vault name /
container layout is unknown.

## Typical workflows

```bash
P=~/.local/share/uv/tools/pymobiledevice3/bin/python

# 0. Smoke test: are we connected and inspecting?
$P obsidian_inspect_demo.py

# 1. Build the plugin locally (skip the unrelated e2e typecheck), then deploy:
( cd ~/Developer/PodNotes && ./node_modules/.bin/vite build )
$P podnotes_ios.py deploy           # AFC push + reload (disable/enable)

# 2. Poke at live state:
$P podnotes_ios.py eval 'app.vault.getName()'
$P podnotes_ios.py eval 'Object.keys(app.plugins.plugins)'

# 3. Reproduce / verify a download bug on the phone:
$P podnotes_ios.py verify            # single download, watch it stream to disk
$P podnotes_ios.py repro --count 8   # stack concurrent downloads to force OOM
```

To target a **specific** episode (reproduce a user's exact scenario), the plugin
exposes no current-episode setter, so mutate the live one in place, then fire the
download command:

```js
// via: podnotes_ios.py eval '<this>'
(()=>{const cur=app.plugins.plugins.podnotes.api.podcast;
  window.__origEp=JSON.parse(JSON.stringify(cur));      // snapshot to restore later
  cur.title="..."; cur.podcastName="..."; cur.streamUrl="https://.../ep.mp3"; cur.mediaType="audio";
  return cur.title})()
// ...then: executeCommandById('podnotes:download-playing-episode')
// ...restore: Object.assign(app.plugins.plugins.podnotes.api.podcast, window.__origEp)
```

## Key learnings / gotchas

- **No remote-debugging port.** Use `pymobiledevice3`'s `webinspector` (iOS 17+);
  `ios-webkit-debug-proxy` is dead on modern iOS.
- **Push files via AFC, not JS.** Injecting a 350 KB `main.js` as a JS string
  through the inspector is painfully slow / hangs. AFC (`apps push` /
  `house_arrest` `set_file_contents`) is fast and robust. The vault is at
  `/Documents/<vault>/.obsidian/plugins/<id>/`.
- **Reload = disable + enable.** `app.plugins.disablePlugin(id)` then
  `enablePluginAndSave(id)` (or `enablePlugin`). After writing files, call
  `app.plugins.loadManifests()` first so a newly-added manifest is registered.
- **`app.vault.adapter.appendBinary` exists at runtime** on the mobile
  CapacitorAdapter (and desktop), though it's missing from Obsidian's public
  `DataAdapter` types. It's the key to streaming large downloads to disk in
  chunks (write first chunk with `writeBinary`, append the rest) instead of
  buffering the whole file in memory — which was the OOM root cause.
- **eval quirk:** the bundled pymobiledevice3's `runtime_evaluate(return_by_value=True)`
  throws `KeyError: 'preview'` on JS `null`. Work around it by `JSON.stringify`-ing
  on the device side and `json.loads`-ing back (see `ev()` in `podnotes_ios.py`).
- **Async eval:** WebKit doesn't reliably await promises via `runtime_evaluate`, so
  `ev()` uses a stash-and-poll pattern (`window.__pmd_r`) to resolve async results.
- **Crash signal:** when the WebView content process is OOM-killed, the inspector
  session goes unreachable — that's the observable. Device crash logs
  (`pymobiledevice3 crash ls`, look for `JetsamEvent`) corroborate native kills.
- **Detecting reproduction safely:** a single large download may survive on a
  high-RAM phone; stacking concurrent downloads (`repro --count N`) amplifies the
  memory pressure to force the kill deterministically.

## Android (CDP)

Android Obsidian is the same Capacitor WebView, but the WebView is **Chromium**
and speaks **standard Chrome DevTools Protocol** — simpler than iOS's WIP, and it
exposes `performance.memory` (real JS-heap tracing, which iOS/WebKit doesn't).
No physical device needed — an emulator works.

Setup (Apple Silicon):

```bash
brew install --cask android-commandlinetools          # needs a JDK (brew install --cask temurin)
export ANDROID_SDK_ROOT=/opt/homebrew/share/android-commandlinetools
yes | sdkmanager --licenses
sdkmanager "platform-tools" "emulator" "platforms;android-34" \
           "system-images;android-34;google_apis;arm64-v8a"
echo no | avdmanager create avd -n podtest -d pixel_6 \
           -k "system-images;android-34;google_apis;arm64-v8a"
"$ANDROID_SDK_ROOT/emulator/emulator" -avd podtest -no-window -no-audio -gpu swiftshader_indirect &
ADB="$ANDROID_SDK_ROOT/platform-tools/adb"
"$ADB" wait-for-device                                 # then poll getprop sys.boot_completed = 1
"$ADB" install -r Obsidian-<ver>.apk                   # github.com/obsidianmd/obsidian-releases
"$ADB" shell am start -n md.obsidian/.MainActivity
```

Attach to the WebView over CDP:

```bash
PID=$("$ADB" shell pidof md.obsidian | tr -d '\r')
"$ADB" forward tcp:9333 localabstract:webview_devtools_remote_$PID   # NOT 9222 — Electron apps (Griply!) grab it
curl -s localhost:9333/json | python3 -m json.tool                  # the page's webSocketDebuggerUrl
uv run --no-project --with websockets python android_cdp.py eval 'app.vault.getName()'
```

Gotchas specific to Android:
- **Restricted mode blocks community plugins.** `enablePlugin` adds to the list but won't instantiate. First run `app.plugins.setEnable(true)`, then enable.
- **Vault path** (App-storage vault) is `/sdcard/Android/data/md.obsidian/files/<vault>`; deploy a build with `adb push build/main.js <vault>/.obsidian/plugins/<id>/main.js`.
- Obsidian's onboarding is itself a WebView, so you can drive vault creation by clicking DOM elements via `android_cdp.py eval`.

Android scripts:
- `android_cdp.py` — general CDP eval client (`eval "<js>"`, `pages`). The Android analogue of the iOS inspector connection.
- `provider_matrix.py` — survey many podcast hosts (redirect depth, Range support, size) via the iTunes Search API. Provider-agnostic; good for not overfitting download tests to one host.
- `android_dl_monitor.py` / `android_timed_download.py` — PodNotes-specific download harnesses: fire the download and trace `performance.memory` + on-disk growth + wall-time + crash. Worked examples.

What the Android trace settled (2026-06): the streamed download is **memory-clean** on Android too — `appendBinary` is O(1), JS heap stays flat (~22 MB) through a 47–155 MB download, no crash. Chunk size barely affects wall-time (connection reuse amortizes the redirect chains), so it's not a slowness lever.

## Safety

These scripts read and (for `deploy`) write files in the live vault and can
trigger downloads. Tests download real episodes; clean up afterward
(`adapter.remove`) and restore any mutated `currentEpisode`. Treat the phone's
vault as production data. The `provider_matrix` / timed-download tools hit real
podcast CDNs and trigger their download analytics — use sparingly.
