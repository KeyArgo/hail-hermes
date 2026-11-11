"""The brief fast path: schedule, inbox and "what was I doing", answered without a model.

These exist because a watch answer that takes fifteen seconds is not an answer. The tests pin the two
things that make a fast path safe: it must answer the questions it owns, and it must return None for
everything else so the real agent still gets the turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hail_hermes import brief


def test_first_use_seeds_a_labelled_demo_brief(tmp_path: Path) -> None:
    """A person with no calendar connected must see a real answer, not a guess and not silence."""
    assert not brief.brief_path(tmp_path).exists()
    data = brief.load(tmp_path)
    assert brief.brief_path(tmp_path).is_file()
    assert data["_note"].lower().startswith("demo brief")
    assert data["schedule"] and data["emails"] and data["trip"]


@pytest.mark.parametrize(
    "question, expected",
    [
        ("What is on my schedule today?", "Standup"),
        ("Any important emails?", "ACME"),
        ("What was I working on?", "Hail Hermes"),
        ("Give me the full itinerary for the France trip.", "Paris"),
    ],
)
def test_owned_questions_are_answered(tmp_path: Path, question: str, expected: str) -> None:
    sentence = brief.answer(question, tmp_path)
    assert sentence and expected in sentence


@pytest.mark.parametrize(
    "question",
    [
        "Say one short sentence about snow.",
        "What is the weather model?",
        "Write me a python function to sort a list",
        "How tall is Everest?",
    ],
)
def test_other_questions_are_left_to_the_agent(tmp_path: Path, question: str) -> None:
    assert brief.answer(question, tmp_path) is None


def test_answers_stay_short_enough_to_speak(tmp_path: Path) -> None:
    """The plan is read aloud while the wrist is raised; it has a budget, not a page."""
    for question in ("Give me the full itinerary for the France trip.", "Any important emails?"):
        sentence = brief.answer(question, tmp_path)
        assert sentence is not None
        assert len(sentence) <= 260, f"{question} -> {len(sentence)} chars"


def test_owner_edits_are_used_verbatim(tmp_path: Path) -> None:
    (tmp_path / "brief.json").write_text(
        json.dumps({"schedule": ["Ferry at 7:15"], "emails": [], "work": "the boat"}),
        encoding="utf-8",
    )
    assert "Ferry at 7:15" in (brief.answer("what's on today?", tmp_path) or "")
    assert "boat" in (brief.answer("what was I working on?", tmp_path) or "")
