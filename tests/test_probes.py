"""Probe resolution: filesystem paths, bundled names, and error surface."""
import pytest

from obsidian_mobile_debug import probes


def test_bundled_core_smoke_is_available():
    assert "core_smoke" in probes.bundled_probe_names()


def test_resolve_bundled_by_name():
    path = probes.resolve_probe_path("core_smoke")
    assert path.name == "core_smoke.js"
    assert path.is_file()


def test_resolve_bundled_with_suffix():
    assert probes.resolve_probe_path("core_smoke.js").name == "core_smoke.js"


def test_load_bundled_probe_reads_source():
    source = probes.load_probe("core_smoke")
    assert "ok" in source and "async" in source


def test_resolve_filesystem_path(tmp_path):
    probe = tmp_path / "custom.js"
    probe.write_text("1 + 1", encoding="utf-8")
    assert probes.resolve_probe_path(str(probe)) == probe


def test_filesystem_path_wins_over_bundled_name(tmp_path):
    probe = tmp_path / "core_smoke.js"
    probe.write_text("42", encoding="utf-8")
    assert probes.resolve_probe_path(str(probe)) == probe


def test_unknown_probe_raises_with_hint():
    with pytest.raises(SystemExit) as excinfo:
        probes.resolve_probe_path("does_not_exist")
    assert "core_smoke" in str(excinfo.value)
