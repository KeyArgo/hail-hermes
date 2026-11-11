"""Instant answers about the day, without a model.

Why this exists: a watch answer that takes 15 seconds is not an answer. Every question in this module
is answered from a small local file in well under a second — the same trick that makes the weather
fast path feel instant.

Nothing here invents data at answer time: if ``brief.json`` is missing it is seeded once with a
*labelled* demo brief, so a person with no calendar connected still sees a meaningful answer instead
of a guess. The file is plain JSON and the owner can edit it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Deliberately narrow: a topic word alone must not hijack a question (learned from the weather
# matcher, which answered "say a sentence about snow" with a forecast).
_SCHEDULE = re.compile(
    r"\b(?:schedule|calendar|agenda|appointments?|meetings?|what'?s on|whats on|am i doing today)\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"\b(?:emails?|inbox|unread|mail)\b", re.IGNORECASE)
_WORK = re.compile(
    r"\b(?:what am i working on|what was i working on|where was i|my project|last session)\b",
    re.IGNORECASE,
)
_TRIP = re.compile(
    r"\b(?:itinerary|trip|travel plan|vacation|holiday)\b", re.IGNORECASE
)

DEMO_BRIEF: dict[str, Any] = {
    "_note": "Demo brief. Edit this file to put your real day in; the watch answers from it instantly.",
    # Kept short on purpose: these are spoken aloud, and a demo answer that runs long feels slow
    # even when the first word arrives instantly.
    "schedule": [
        "Standup at 9:30",
        "Dentist at 1",
        "Call with Alex at 4",
    ],
    "emails": [
        "Invoice from ACME",
        "Contract from Ryan to sign",
    ],
    "work": "Hail Hermes, on the watch voice",
    "trip": [
        "Three days in Paris in May",
        "Markets and a bistro on day one",
        "Louvre and the Marais on day two",
        "Versailles on day three",
    ],
}


def brief_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "brief.json"


def load(state_dir: str | Path) -> dict[str, Any]:
    """Read the brief, seeding the labelled demo one on first use."""
    path = brief_path(state_dir)
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEMO_BRIEF, indent=2), encoding="utf-8")
        except OSError:
            return dict(DEMO_BRIEF)
        return dict(DEMO_BRIEF)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(DEMO_BRIEF)
    except (OSError, json.JSONDecodeError):
        return dict(DEMO_BRIEF)


def _items(data: dict[str, Any], key: str) -> list[str]:
    raw = data.get(key)
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(row).strip() for row in raw if str(row).strip()]
    return []


def _spoken_list(items: list[str], limit: int = 4) -> str:
    items = items[:limit]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _cap(text: str, limit: int) -> str:
    """Trim a spoken sentence at a clause boundary; speech has a budget, not a page."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", ", "):
        idx = cut.rfind(sep)
        if idx > limit // 2:
            return cut[: idx + len(sep)].rstrip(". ") + "."
    return cut.rsplit(" ", 1)[0] + "."


def answer(text: str, state_dir: str | Path) -> str | None:
    """A spoken sentence for a brief question, or ``None`` to let the agent answer."""
    text = (text or "").strip()
    if not text:
        return None
    data = load(state_dir)

    if _TRIP.search(text):
        steps = _items(data, "trip")
        if steps:
            # A plan read aloud has to finish while the wrist is still raised: join clause by clause
            # and stop at the spoken-length budget rather than listing everything.
            body = ", then ".join(_cap(step, 46) for step in steps[:4])
            return _cap(f"Here's the plan: {body}.", 190)
    if _SCHEDULE.search(text):
        rows = _items(data, "schedule")
        if rows:
            return f"Today: {_spoken_list(rows)}."
        return "Nothing on the calendar today."
    if _EMAIL.search(text):
        rows = _items(data, "emails")
        if rows:
            return _cap(
                f"{len(rows)} things worth a look: {_spoken_list(rows, limit=2)}.", 120
            )
        return "Nothing new in the inbox."
    if _WORK.search(text):
        work = str(data.get("work") or "").strip()
        if work:
            return f"You were on {work}."
    return None
