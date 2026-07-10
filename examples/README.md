# Examples - historical campaign scripts

These scripts predate the `omd` CLI. They are the plugin-specific scratch tools
that the generalized CLI was distilled from, kept verbatim because each one
documents a real on-device debugging campaign (mostly the PodNotes mobile OOM
investigation). They are **recipes, not supported entry points** - for everyday
work use `omd ios ...` / `omd android ...` instead.

Each still runs standalone the way it always did, e.g.:

```bash
uv run --no-project --with pymobiledevice3 python examples/podnotes_ios.py pages
uv run --no-project --with websockets python examples/android_dl_monitor.py
```

## What each one is

- **`podnotes_ios.py`** - the original iOS harness with `BUNDLE`/`PLUGIN_ID`/`REPO`
  hardcoded at the top, plus two download-specific commands used to chase the OOM
  bug: `repro [--count N]` (stack concurrent downloads to force the crash) and
  `verify` (fire one download and watch the on-disk file grow chunk-by-chunk).
  The template for how to write plugin-specific reproduction commands.
- **`obsidian_inspect_demo.py`** - read-only first-contact smoke test: connect and
  print vault name, file counts, plugins, dark mode, recent notes.
- **`afc_discover.py`** - list the app's `/Documents` container over AFC and locate
  the vault dir that contains `.obsidian`. Use when the vault layout is unknown.
- **`provider_matrix.py`** - survey many podcast hosts (redirect depth, Range
  support, size) via the iTunes Search API, so download tests do not overfit to
  one CDN. Provider-agnostic.
- **`android_dl_monitor.py`** / **`android_timed_download.py`** - PodNotes-specific
  Android download harnesses: fire a download and trace `performance.memory` +
  on-disk growth + wall-time + crash over CDP.
- **`android_waypoint_validate.py`** - instruments the vault event bus
  (`vault.on('create'|'modify'|...)`), fires one download, and reports crash /
  byte-perfection / leftover temp files. Built to prove the PodNotes x Waypoint
  watcher-storm crash and its fix; run it across the provider matrix, old vs new
  build, watcher plugin off vs on.

## Porting a recipe to the CLI

Most of what these scripts do is now one `omd` command:

- connect + eval JS  ->  `omd ios eval '<js>'` / `omd android eval '<js>'`
- deploy a build     ->  `omd ios deploy --plugin <id> --repo <path> --vault <name>`
- reload a plugin     ->  `omd ios reload --plugin <id>`
- stream logs/errors  ->  `omd ios logs` / `omd android logs`

The parts that stay plugin-specific (driving a plugin's own API to reproduce a
bug, tracing `performance.memory` around a download) are exactly what a **probe**
is for: put the JS in a `.js` file and run `omd ios eval --probe my_probe.js`.
See `src/obsidian_mobile_debug/probes/core_smoke.js` for the shape.
