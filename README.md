# obsidian-mobile-debug

An installable CLI (`omd`) plus hard-won notes for **debugging Obsidian (and
other WebView apps) on a physical phone over USB** - driving the page's JS,
deploying a locally-built plugin, reproducing user-reported bugs, and capturing
crashes - all from the Mac terminal. Both platforms, one command surface:

- **`omd ios ...`** - iPhone WKWebView via [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3)
  (WebKit Web Inspector + AFC/house_arrest).
- **`omd android ...`** - Android Chromium WebView via `adb` + Chrome DevTools
  Protocol.

Built while fixing a real PodNotes bug: downloading a large podcast episode
OOM-killed Obsidian on mobile. This kit is what made it possible to reproduce it
on-device, fix it, and verify the fix end-to-end.

## How it works

Obsidian is a Capacitor WebView app on both platforms; the transport differs.

**iOS is a WKWebView.** Two USB channels, both via `pymobiledevice3`, do
everything:

- **WebKit Web Inspector** (`com.apple.webinspector`, over usbmux/lockdown) - open
  an `InspectorSession` against the page and evaluate arbitrary JS against
  Obsidian's `app` API. This is how we read state, enable/reload plugins, and
  trigger actions.
- **AFC / house_arrest** (`com.apple.mobile.house_arrest`, documents mode) - read
  and write files inside the app's container (the vault lives at
  `/Documents/<vault>/`). This is how we push a built plugin onto the phone.

There is **no Chrome-style `--remote-debugging-port`** on Safari/Obsidian; the
inspector protocol is reached over Apple's own transport, which is why
`pymobiledevice3` (not `ios-webkit-debug-proxy`) is the tool - the latter doesn't
support iOS 17+.

**Android is a Chromium WebView** that speaks **standard Chrome DevTools
Protocol** - simpler than iOS's WIP, and it exposes `performance.memory` (real
JS-heap tracing, which iOS/WebKit doesn't). No physical device needed - an
emulator works. `omd` discovers the app's `webview_devtools_remote_<pid>` unix
socket, `adb forward`s it to a local port, and speaks CDP over the resulting
websocket. It sets up and tears down the forward for you.

## Install

```bash
uv tool install obsidian-mobile-debug     # or, from a checkout: uv tool install .
```

That puts `omd` on your PATH with its dependencies (`pymobiledevice3`,
`websockets`) isolated. No global install needed for one-offs:

```bash
uv run --with obsidian-mobile-debug omd ios pages
```

### iOS prerequisites

- macOS, iPhone connected via USB and unlocked/trusted.
- On the phone: **Settings -> Apps -> Safari -> Advanced -> Web Inspector = ON**
  (iOS 18+ path). The target app's WebView must be `isInspectable` - Obsidian is.
- On **iOS 17+ the webinspector + AFC services work directly over usbmux - no
  sudo RemoteXPC tunnel needed** (unlike most other developer services).

### Android prerequisites

- `adb` on your PATH (or set `ADB=/path/to/adb`). Obsidian running on a device or
  emulator, with the vault you intend to touch open.
- To stand up an emulator + Obsidian + CDP from scratch, `./android_setup.sh`
  does the whole thing (SDK, headless emulator, APK install, forward); re-runnable
  and reversible with `REMOVE_SDK=1 ./android_teardown.sh`. The CLI drives an
  already-running WebView; those scripts provision one.

## Quickstart

```bash
# iOS: confirm you're connected and inspecting, then smoke-test the runtime.
omd ios pages
omd ios diagnose
omd ios eval --probe core_smoke        # bundled, plugin-agnostic health check

# Android: same surface. The CDP forward is set up and torn down automatically.
omd android diagnose
omd android eval --probe core_smoke
```

## Command reference

`--bundle` defaults to `md.obsidian` on both platforms; pass it to target another
WebView app. Android commands take `--port` (default `9333`) for the CDP forward.

### `omd ios <cmd>`

| Command | What it does |
|---|---|
| `pages` | List inspectable pages/webviews on the device. |
| `eval "<js>"` / `eval --probe <file\|name>` | Evaluate an async-aware JS expression (or a probe file) against the page; print JSON. |
| `command <id>` | Run an Obsidian command by id (`executeCommandById`). |
| `diagnose [--plugin <id>]` | Report runtime state (vault, platform, plugin counts); with `--plugin`, add that plugin's install/enable state and its AFC files. |
| `deploy --plugin <id> --repo <path>` | Back up, then AFC-push the build (`main.js` + `manifest.json`, plus `styles.css` if present) into the vault, then reload. |
| `reload --plugin <id>` | disable -> enable the plugin. |
| `logs [--seconds N]` | Stream console + uncaught errors. |
| `restore [--backup <dir>]` | Restore a pre-deploy backup (newest by default). |
| `backups` | List local pre-deploy backups. |

### `omd android <cmd>`

| Command | What it does |
|---|---|
| `pages` | List CDP targets. |
| `eval "<js>"` / `eval --probe <file\|name>` | Evaluate JS over CDP (awaitPromise by default; `--no-await` to disable). |
| `diagnose [--plugin <id>]` | Report runtime state; with `--plugin`, add that plugin's state. |
| `deploy --plugin <id> --repo <path> --vault-path <abs>` | `adb push` the build to `<vault-path>/.obsidian/plugins/<id>`, then reload. |
| `reload --plugin <id>` | `setEnable(true)` then disable -> enable the plugin. |
| `logs [--seconds N]` | Stream logcat lines matching Obsidian/WebView/crash. |

`deploy` derives the build files from `--repo` (`main.js` at the repo root or
`build/main.js`; `manifest.json`; optional `styles.css`); override any with
`--main` / `--manifest` / `--styles`.

## How deploy / reload works

1. **Resolve the build** from `--repo` (or explicit `--main`/`--manifest`/`--styles`).
2. **iOS: back up first.** Before writing, the existing on-device plugin folder is
   pulled to `~/.obsidian-mobile-debug/backups/<device>/<vault>/<plugin>/<timestamp>/`
   and byte-verified against the device (skip with `--no-backup`). Android has no
   backup path - see Safety.
3. **Push the files.** iOS uses AFC `set_file_contents` and re-reads every file to
   confirm byte count + SHA-256 match; Android uses `adb push`. A `.hotreload`
   marker is written (iOS) so pjeby's Hot Reload can watch it.
4. **Load + enable.** `app.plugins.loadManifests()` (so a newly-added manifest is
   registered), then disable -> `enablePluginAndSave` (falling back to
   `enablePlugin`). On Android, `app.plugins.setEnable(true)` runs first because
   Restricted Mode adds the id to the enabled list but won't instantiate the plugin.
5. **Verify.** The runtime state after deploy is printed; a non-instantiated plugin
   yields a non-zero exit code.

`reload` is just step 4 against the already-installed plugin.

## Safety model

The phone vault is real data. Guards, strongest first:

- **`--confirm-real-vault`.** `deploy` and `reload` refuse to touch a vault whose
  name doesn't look like a test vault (contains none of `test`, `scratch`,
  `debug`, `sandbox`) unless you pass `--confirm-real-vault`, or whitelist the
  exact name with `--test-vault <name>`.
- **Open-vault match (iOS/Android deploy).** Deploy refuses unless the vault open
  in the WebView is the same one the files are being written to, so you can't push
  into a background vault by accident.
- **Backup + byte-verify (iOS).** Every deploy snapshots the existing plugin
  folder and verifies it before writing, and verifies each pushed file after.
  `restore` puts it back (and re-checks the device id and vault path first).
- **Android has no backup/restore path.** `adb push` is not snapshot-verified, so
  Android `deploy` is intended for a **disposable scratch vault** and always
  requires the confirm flag for a non-test-named vault.

Set `OMD_BACKUP_DIR` to relocate the backup root.

## Agent usage

Everything prints JSON to stdout, and exit codes are meaningful:

- **`0`** - success.
- **`1`** - usage/connection/device error (the message names the fix).
- **`2`** - the command ran but the result is a failure: an `eval`/probe that
  returned `{ "ok": false, ... }`, or a `deploy` where the plugin didn't
  instantiate.

So a probe doubles as a CI/agent gate:

```bash
omd ios eval --probe ./probes/my_check.js || echo "probe failed"
```

Write probes as `.js` files (a bare expression or an async IIFE) and pass them
with `--probe`. A path wins; otherwise the name resolves against the bundled
probes. `core_smoke` is bundled as a plugin-agnostic starting point - see
`src/obsidian_mobile_debug/probes/core_smoke.js`. The iOS eval transfers the
source base64-chunked and awaits it via a stash-and-poll runner, so large,
multi-statement probes work despite WebKit's `runtime_evaluate` quirks.

## Examples

`examples/` holds the original plugin-specific scratch scripts (the PodNotes OOM
campaign, provider surveys, the Waypoint watcher-storm validator). They predate
the CLI and are kept verbatim as recipes; see `examples/README.md`.

## Key learnings / gotchas

- **No remote-debugging port on iOS.** Use `pymobiledevice3`'s `webinspector`
  (iOS 17+); `ios-webkit-debug-proxy` is dead on modern iOS.
- **Push files via AFC, not JS.** Injecting a 350 KB `main.js` as a JS string
  through the inspector is painfully slow / hangs. AFC (`house_arrest`
  `set_file_contents`) is fast and robust. The vault is at
  `/Documents/<vault>/.obsidian/plugins/<id>/`.
- **Reload = disable + enable.** `app.plugins.disablePlugin(id)` then
  `enablePluginAndSave(id)` (or `enablePlugin`). After writing files, call
  `app.plugins.loadManifests()` first so a newly-added manifest is registered.
- **Android Restricted Mode blocks community plugins.** `enablePlugin` adds to
  the list but won't instantiate. Run `app.plugins.setEnable(true)` first (deploy
  and reload do this for you).
- **`app.vault.adapter.appendBinary` exists at runtime** on the mobile
  CapacitorAdapter (and desktop), though it's missing from Obsidian's public
  `DataAdapter` types. It's the key to streaming large downloads to disk in
  chunks (write first chunk with `writeBinary`, append the rest) instead of
  buffering the whole file in memory - which was the OOM root cause.
- **iOS eval quirk:** the bundled pymobiledevice3's `runtime_evaluate(return_by_value=True)`
  throws `KeyError: 'preview'` on JS `null`. Work around it by `JSON.stringify`-ing
  on the device side and `json.loads`-ing back.
- **Async eval:** WebKit doesn't reliably await promises via `runtime_evaluate`, so
  `ev()` uses a base64-chunk + stash-and-poll pattern (`window.__omd_r`) to resolve
  async results.
- **iOS crash signal:** when the WebView content process is OOM-killed, the
  inspector session goes unreachable - that's the observable. Device crash logs
  (`pymobiledevice3 crash ls`, look for `JetsamEvent`) corroborate native kills.
- **Android vault paths vary.** App-storage vaults live at
  `/sdcard/Android/data/md.obsidian/files/<vault>`; a `/sdcard/Documents/<vault>`
  scratch vault is easier to `adb push` into. Pass the absolute path via
  `--vault-path`.
- **Don't grab port 9222.** Electron apps (Griply!) squat on it; `omd` defaults to
  `9333`.

### What the Android trace settled (2026-06)

In a **clean** vault the streamed download is memory-clean on Android -
`appendBinary` is O(1), JS heap stays flat (~22 MB) through a 47-155 MB download,
no crash. That clean-vault result is also what made the real bug *hard* to see:
the reported crash only reproduces with a **vault-watcher plugin installed**.
Appending each chunk to the final vault path fires ~1 create + ~11 modify events
on the growing file; Waypoint (and any `vault.on('modify')` plugin - Dataview,
Obsidian Git) calls `cachedRead` on each event with no extension filter, loading
the growing media file into memory over and over -> OOM. PodNotes 2.17.2 fixes it
by streaming to a dot-prefixed temp (which Obsidian keeps out of the file index,
so it fires zero watcher events) then `adapter.rename`-ing it into place. The
`examples/android_waypoint_validate.py` recipe measured the event count drop from
~12 to 1 and the crash disappearing with Waypoint enabled.
