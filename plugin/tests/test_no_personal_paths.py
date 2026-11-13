"""Keep one person's machine out of the repository.

The owner's rule is explicit: server addresses, home-network IPs and personal file paths do not
belong in this tree. Nothing else enforces it — a path pasted into a helper script during a debugging
session looks exactly like code, and it survives review because it reads as an example.

So the rule is a test. It scans the tracked tree for the shapes that gave away a real machine here:
absolute Windows user directories, this host's home root, and private IPv4 ranges. Test-net ranges
(RFC 5737) and the emulator alias `10.0.2.2` are allowed, because they are documentation, not anyone.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "build", ".gradle", "dist"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".apk", ".wav", ".mp3", ".zip", ".jar", ".so"}

# Windows home directories and this host's own roots: personal, never a fixture.
PERSONAL = [
    re.compile(r"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+"),
    re.compile(r"[\\/]c[\\/]Users[\\/][A-Za-z0-9._-]+"),
    re.compile(r"/mnt/homes/"),
]

# Private ranges that identify a home LAN. Test-net (192.0.2/24, 198.51.100/24, 203.0.113/24) and the
# Android emulator's 10.0.2.2 are deliberately absent from this list.
PRIVATE_IP = re.compile(
    r"\b(?:"
    r"192\.168\.\d{1,3}\.\d{1,3}"
    r"|10\.(?!0\.2\.2\b)\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")\b"
)


def _files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        # The guard itself names the shapes it looks for.
        if path.name == Path(__file__).name:
            continue
        yield path


def _scan(pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()[:120]}")
    return hits


def test_no_personal_file_paths() -> None:
    hits = [h for pattern in PERSONAL for h in _scan(pattern)]
    assert not hits, "personal paths in the tree:\n" + "\n".join(hits)


def test_no_home_network_addresses() -> None:
    hits = _scan(PRIVATE_IP)
    assert not hits, "private/home-network addresses in the tree:\n" + "\n".join(hits)
