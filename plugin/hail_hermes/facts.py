"""Facts the owner asked Hail Hermes to remember — kept in a file this plugin owns.

Review item 3. The plugin used to append straight into Hermes' own `MEMORY.md`. That file belongs to
core: it has an entry format, a size budget and a safety scan, and a plugin writing into it bypasses all
three. A fact remembered by voice also ended up in the agent's long-term memory without ever passing
through the tool that is supposed to own that decision.

So the facts live here instead — one bullet per line, in the plugin's own state directory — and only the
quick backend reads them. The trade is deliberate and stated in the README: these are Hail Hermes' facts,
not the agent's; to change what the agent remembers, say it in a normal turn.

The file is small by construction: facts are capped, deduplicated case-insensitively, and the newest
`MAX_FACTS` are kept.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import filelock

FACTS_FILENAME = "facts.md"
MAX_FACT_CHARS = 300
MAX_FACTS = 200


def facts_path(state_dir: str | Path) -> Path:
    """Where the plugin keeps what it was asked to remember."""
    return Path(state_dir).expanduser() / FACTS_FILENAME


def read_facts(state_dir: str | Path) -> list[str]:
    """Every remembered fact, oldest first. A missing file is an empty memory, not an error."""
    try:
        raw = facts_path(state_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    facts = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        if line:
            facts.append(line)
    return facts


def append_fact(fact: str, state_dir: str | Path) -> Path:
    """Append one fact and return the file. Raises ValueError for an empty or duplicate fact.

    The read, the append and the trim all happen under one lock on one handle. Trimming used to run after
    the lock was released and rewrote the file from a separate handle, so two callers — the dashboard and a
    voice command, say — could interleave: one trims a snapshot that predates the other's append, and that
    fact is gone (quality review). At MAX_FACTS this is not a rare race; it is every write.
    """
    cleaned = " ".join(str(fact).strip().split())[:MAX_FACT_CHARS].strip()
    if not cleaned:
        raise ValueError("memory fact must not be empty")
    target = facts_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a+", encoding="utf-8") as fh:
        filelock.lock(fh)
        try:
            fh.seek(0)
            existing = [line.strip()[2:].strip().lower()
                        for line in fh.read().splitlines() if line.strip().startswith("- ")]
            if cleaned.lower() in existing:
                return target
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            needs_newline = False
            if size > 0:
                fh.seek(size - 1, os.SEEK_SET)
                needs_newline = fh.read(1) != "\n"
            fh.write(("\n" if needs_newline else "") + f"- {cleaned}\n")
            fh.flush()
            _trim_locked(fh)
        finally:
            filelock.unlock(fh)
    return target


def facts_block(state_dir: str | Path) -> str:
    """The facts as a prompt section, or an empty string when there are none."""
    facts = read_facts(state_dir)
    if not facts:
        return ""
    return "Facts the owner asked you to remember:\n" + "\n".join(f"- {fact}" for fact in facts)


def _trim_locked(fh) -> None:
    """Keep the newest MAX_FACTS lines so a long-lived install cannot grow without bound.

    Called with the caller's lock already held and the same handle: the rewrite is part of the append, not
    a follow-up that another writer can slip in front of.
    """
    fh.seek(0)
    lines = [line for line in fh.read().splitlines() if line.strip()]
    if len(lines) <= MAX_FACTS:
        return
    fh.seek(0)
    fh.truncate()
    fh.write("\n".join(lines[-MAX_FACTS:]) + "\n")
    fh.flush()
