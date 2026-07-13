"""Cross-process Web Inspector session lock (one OMD per device + bundle).

webinspectord tolerates only one useful OMD client per app page: a second
process connecting while another streams logs just hangs until the first
exits, which reads like a device or plugin failure. This lock makes the
second process fail fast instead, naming the owner and how to proceed.

The lock is an ``flock(2)``-ed file keyed by device UDID + bundle id under
``~/.obsidian-mobile-debug/locks`` (override: ``OMD_LOCK_DIR``). ``flock``
gives the two properties that matter for free:

- stale-lock recovery: the kernel drops the lock when the owner dies (even
  SIGKILL), so a leftover file from a crashed run never blocks anyone;
- guaranteed release: normal exit, exceptions, and interrupts all close the
  fd, which releases the lock.

The file's JSON body (pid / command / start time) is informational only - it
identifies the owner in the contention error; the flock state is what locks.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _safe_segment(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def lock_root() -> Path:
    return Path(
        os.environ.get("OMD_LOCK_DIR", Path.home() / ".obsidian-mobile-debug" / "locks")
    ).expanduser()


def lock_path(device_id: str, bundle: str) -> Path:
    return lock_root() / f"{_safe_segment(device_id)}--{_safe_segment(bundle)}.lock"


def _owner_info() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "command": " ".join(sys.argv),
        "startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _read_owner(fd: int) -> dict[str, Any]:
    """Best-effort read of the owner JSON; the owner may be mid-write."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 4096)
        owner = json.loads(data.decode("utf-8"))
        if isinstance(owner, dict):
            return owner
    except (OSError, ValueError):
        pass
    return {}


def contention_message(device_id: str, bundle: str, owner: dict[str, Any]) -> str:
    pid = owner.get("pid")
    lines = [
        f"Another OMD process already owns the Web Inspector session for device "
        f"{device_id!r} / bundle {bundle!r}.",
        f"Owner: pid {pid or 'unknown'}"
        + (f", started {owner['startedAt']}" if owner.get("startedAt") else ""),
    ]
    if owner.get("command"):
        lines.append(f"Command: {owner['command']}")
    lines.append(
        "Wait for it to finish (e.g. a running `omd ios logs --seconds N`) or stop it"
        + (f" (kill {pid})" if pid else "")
        + ", then retry."
    )
    return "\n".join(lines)


@contextlib.contextmanager
def inspector_lock(device_id: str, bundle: str):
    """Hold the per-device/bundle inspector lock for the duration of the block.

    Raises ``SystemExit`` (non-zero) immediately when another live process
    holds it, reporting that owner's pid, command, and start time.
    """
    import fcntl

    path = lock_path(device_id, bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(contention_message(device_id, bundle, _read_owner(fd))) from None
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (json.dumps(_owner_info(), indent=2) + "\n").encode("utf-8"))
        yield
    finally:
        # Closing the fd releases the flock; the leftover file is inert (its
        # JSON is stale info, the next owner overwrites it after locking).
        os.close(fd)
