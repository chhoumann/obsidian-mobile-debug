"""Inspector session lock: contention, stale-lock recovery, release (issue #6)."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from obsidian_mobile_debug import lock


@pytest.fixture()
def lock_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OMD_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def test_lock_path_is_keyed_by_device_and_bundle(lock_dir):
    path = lock.lock_path("00008150-001C", "md.obsidian")
    assert path.parent == lock_dir
    assert path.name == "00008150-001C--md.obsidian.lock"


def test_lock_path_sanitizes_unsafe_segments(lock_dir):
    assert lock.lock_path("a/b c", "x:y").name == "a-b-c--x-y.lock"


def test_acquire_writes_owner_info(lock_dir):
    with lock.inspector_lock("dev", "bundle"):
        owner = json.loads(lock.lock_path("dev", "bundle").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["command"]
        assert owner["startedAt"]


def test_contention_fails_fast_with_owner_details(lock_dir):
    """flock is per open-file-description, so a second open in this process contends."""
    with lock.inspector_lock("dev", "bundle"):
        with pytest.raises(SystemExit) as excinfo:
            with lock.inspector_lock("dev", "bundle"):
                pass
    message = str(excinfo.value)
    assert f"pid {os.getpid()}" in message
    assert "Command:" in message
    assert "retry" in message


def test_different_bundle_does_not_contend(lock_dir):
    with lock.inspector_lock("dev", "md.obsidian"):
        with lock.inspector_lock("dev", "other.bundle"):
            pass


def test_lock_released_after_normal_exit(lock_dir):
    with lock.inspector_lock("dev", "bundle"):
        pass
    with lock.inspector_lock("dev", "bundle"):
        pass


def test_lock_released_after_exception(lock_dir):
    with pytest.raises(RuntimeError):
        with lock.inspector_lock("dev", "bundle"):
            raise RuntimeError("boom")
    with lock.inspector_lock("dev", "bundle"):
        pass


def test_stale_lock_file_from_dead_process_is_recovered(lock_dir):
    """A leftover file whose writer died holds no flock, so acquisition succeeds."""
    path = lock.lock_path("dev", "bundle")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 999999999, "command": "omd ios logs"}), encoding="utf-8")
    with lock.inspector_lock("dev", "bundle"):
        owner = json.loads(path.read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()


def test_corrupt_lock_file_reports_unknown_owner(lock_dir):
    path = lock.lock_path("dev", "bundle")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    with lock.inspector_lock("dev", "bundle"):
        with pytest.raises(SystemExit) as excinfo:
            with lock.inspector_lock("dev", "bundle"):
                pass
    # Owner info is overwritten by the live holder, so it is known here; the
    # corrupt-content path is exercised via _read_owner directly below.
    assert "Another OMD process" in str(excinfo.value)


def test_read_owner_tolerates_garbage(lock_dir):
    path = lock_dir / "x.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe garbage")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert lock._read_owner(fd) == {}
    finally:
        os.close(fd)


def test_contention_message_without_owner_info():
    message = lock.contention_message("dev", "bundle", {})
    assert "pid unknown" in message
    assert "retry" in message


def test_cross_process_contention(lock_dir):
    """A real second process must fail fast with the owner's pid in the message."""
    child = textwrap.dedent("""
        import sys
        from obsidian_mobile_debug.lock import inspector_lock
        try:
            with inspector_lock("dev", "bundle"):
                pass
        except SystemExit as e:
            print(e)
            sys.exit(3)
        sys.exit(0)
    """)
    with lock.inspector_lock("dev", "bundle"):
        result = subprocess.run(
            [sys.executable, "-c", child], text=True, capture_output=True,
            env={**os.environ, "OMD_LOCK_DIR": str(lock_dir)},
        )
    assert result.returncode == 3
    assert f"pid {os.getpid()}" in result.stdout
