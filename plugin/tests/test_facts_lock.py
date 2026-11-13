"""The facts file survives concurrent writers once it reaches its limit.

Quality review: `append_fact` released the lock before trimming, and the trim rewrote the file from a
second handle. Two writers — the dashboard route and a voice command, which is the ordinary case — could
then interleave so that one trimmed a snapshot taken before the other's append, silently dropping a fact
the user had just been told was remembered. At the 200-fact limit that is not a rare race: it is every
write.

These writers are real threads doing real appends, not a mock: the lock is on separate file handles, which
is exactly the situation `filelock` exists for.
"""

from __future__ import annotations

import threading

from hail_hermes import facts
from hail_hermes.facts import MAX_FACTS, append_fact, facts_path, read_facts


def _seed(state_dir, count: int) -> None:
    path = facts_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"- seed {i}\n" for i in range(count)), encoding="utf-8")


def test_concurrent_appends_at_the_limit_lose_nothing(tmp_path):
    _seed(tmp_path, MAX_FACTS - 1)
    new = [f"concurrent fact {i}" for i in range(8)]
    errors: list[BaseException] = []
    started = threading.Barrier(len(new))

    def write(fact: str) -> None:
        try:
            started.wait(timeout=10)
            append_fact(fact, tmp_path)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(fact,)) for fact in new]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    stored = read_facts(tmp_path)
    assert len(stored) == MAX_FACTS, f"expected the file to stay at its limit, got {len(stored)}"
    missing = [fact for fact in new if fact not in stored]
    assert not missing, f"appends lost to the trim race: {missing}"


def test_a_reader_never_sees_a_partial_fact(tmp_path):
    """The product's own reader takes the lock, so it never reports a fragment of a line.

    An append writes one line over several bytes, and a trim rewrites the whole file in place: without the
    lock a reader can catch either halfway. `read_facts` is what the pane, the dashboard and quick mode all
    use, so this is the guarantee that matters.
    """
    _seed(tmp_path, MAX_FACTS)
    stop = threading.Event()
    torn: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            for fact in read_facts(tmp_path):
                # A fact is a whole sentence here; a fragment would be a prefix of one.
                if not fact.startswith(("seed ", "tail fact ")) and fact != "the newest fact":
                    torn.append(fact)
                    return

    def writer() -> None:
        for i in range(30):
            append_fact(f"tail fact {i}", tmp_path)

    watch = threading.Thread(target=reader)
    watch.start()
    try:
        writer()
    finally:
        stop.set()
        watch.join(timeout=10)

    assert not torn, f"a reader saw a partial fact: {torn[:3]}"
    assert len(read_facts(tmp_path)) == MAX_FACTS


def test_the_finished_file_is_whole_lines(tmp_path):
    """After the work is done, every line is a fact and the file ends cleanly."""
    _seed(tmp_path, MAX_FACTS)
    for i in range(12):
        append_fact(f"tail fact {i}", tmp_path)
    raw = facts_path(tmp_path).read_bytes()
    assert raw.endswith(b"\n")
    for line in raw.decode("utf-8").splitlines():
        assert line.startswith("- ") and len(line) > 2, line


def test_the_limit_still_drops_the_oldest_fact(tmp_path):
    _seed(tmp_path, MAX_FACTS)
    append_fact("the newest fact", tmp_path)
    stored = read_facts(tmp_path)
    assert len(stored) == MAX_FACTS
    assert stored[-1] == "the newest fact"
    assert "seed 0" not in stored, "the oldest fact should be the one that goes"
    assert facts.MAX_FACTS == 200
