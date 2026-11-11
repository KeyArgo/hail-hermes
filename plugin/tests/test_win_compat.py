"""Windows-compat: cross-platform file locks behave like the old fcntl locks."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hail_hermes import filelock


def test_lock_unlock_round_trip(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    with lock_path.open("a+") as handle:
        filelock.lock(handle)
        handle.write("payload\n")
        handle.flush()
        filelock.unlock(handle)
    assert lock_path.read_text() == "payload\n"


def test_lock_reentrant_after_unlock(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    with lock_path.open("a+") as handle:
        filelock.lock(handle)
        filelock.unlock(handle)
        filelock.lock(handle)
        filelock.unlock(handle)


def test_threads_contend_for_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "contend.lock"
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def first() -> None:
        with lock_path.open("a+") as handle:
            filelock.lock(handle)
            order.append("first")
            entered.set()
            release.wait(5)
            filelock.unlock(handle)

    def second() -> None:
        entered.wait(5)
        with lock_path.open("a+") as handle:
            filelock.lock(handle)
            order.append("second")
            filelock.unlock(handle)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(5)
        time.sleep(0.05)
        release.set()
    finally:
        for thread in threads:
            thread.join(timeout=10)

    assert order == ["first", "second"]


def test_unlock_after_position_moves(tmp_path: Path) -> None:
    lock_path = tmp_path / "move.lock"
    with lock_path.open("a+") as handle:
        filelock.lock(handle)
        handle.write("x" * 100)
        handle.flush()
        handle.seek(0)
        filelock.unlock(handle)
    with lock_path.open("a+") as second:
        filelock.lock(second)  # raises if the first unlock missed its byte
        filelock.unlock(second)


def test_lock_does_not_block_readers(tmp_path: Path) -> None:
    data_path = tmp_path / "data.bin"
    data_path.write_bytes(b"payload")
    lock_path = tmp_path / "data.lock"
    with lock_path.open("a+") as handle:
        filelock.lock(handle)
        try:
            assert data_path.read_bytes() == b"payload"
            with lock_path.open("a+") as reader:
                assert reader.read() == ""
        finally:
            filelock.unlock(handle)
