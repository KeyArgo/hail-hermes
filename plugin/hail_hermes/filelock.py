"""Cross-platform advisory file locks.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` with a retry loop
(the msvcrt variant fails immediately instead of blocking). Both operate on a
file descriptor or file object and are best-effort advisory locks. On Windows
the lock is always taken at a fixed far offset so the current stream position
never affects which byte is locked and data reads stay unblocked.
"""

from __future__ import annotations

import os
import time
from typing import IO

__all__ = ["lock", "unlock"]

_POSIX = os.name == "posix"

if _POSIX:
    import fcntl  # noqa: F401  (used only on POSIX)

    def lock(target: int | IO[str]) -> None:
        fcntl.flock(_fileno(target), fcntl.LOCK_EX)

    def unlock(target: int | IO[str]) -> None:
        fcntl.flock(_fileno(target), fcntl.LOCK_UN)

else:
    import msvcrt

    # A far offset past any real data: Windows locks are enforced, so a lock on
    # a data byte would block readers. Keeping it fixed means the stream
    # position never changes which byte gets locked.
    _LOCK_OFFSET = 0x7FFFFFF0

    def _lock_at(fd: int, mode: int) -> None:
        saved = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, saved, os.SEEK_SET)

    def lock(target: int | IO[str]) -> None:
        fd = _fileno(target)
        deadline = time.monotonic() + 30.0
        delay = 0.005
        while True:
            try:
                _lock_at(fd, msvcrt.LK_LOCK)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(0.05, delay * 2)

    def unlock(target: int | IO[str]) -> None:
        _lock_at(_fileno(target), msvcrt.LK_UNLCK)


def _fileno(target: int | IO[str]) -> int:
    return target if isinstance(target, int) else target.fileno()
