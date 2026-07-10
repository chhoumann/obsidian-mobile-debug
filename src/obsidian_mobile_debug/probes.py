"""Probe resolution shared by the iOS and Android eval commands.

A "probe" is just a JavaScript file evaluated against the Obsidian page. The
loader accepts either a filesystem path or the bare name of a probe bundled with
this package (``core_smoke`` -> ``probes/core_smoke.js``).
"""
from __future__ import annotations

from pathlib import Path

BUNDLED_PROBES_DIR = Path(__file__).resolve().parent / "probes"


def bundled_probe_names() -> list[str]:
    if not BUNDLED_PROBES_DIR.is_dir():
        return []
    return sorted(path.stem for path in BUNDLED_PROBES_DIR.glob("*.js"))


def resolve_probe_path(probe: str) -> Path:
    """Map a probe reference to a concrete .js file.

    Resolution order: an existing filesystem path wins; otherwise the reference
    is treated as the name of a bundled probe (with or without the .js suffix).
    """
    candidate = Path(probe).expanduser()
    if candidate.is_file():
        return candidate

    name = probe[:-3] if probe.endswith(".js") else probe
    bundled = BUNDLED_PROBES_DIR / f"{name}.js"
    if bundled.is_file():
        return bundled

    available = ", ".join(bundled_probe_names()) or "(none)"
    raise SystemExit(
        f"Probe {probe!r} not found. Pass a path to a .js file, or one of the "
        f"bundled probes: {available}"
    )


def load_probe(probe: str) -> str:
    return resolve_probe_path(probe).read_text(encoding="utf-8")
