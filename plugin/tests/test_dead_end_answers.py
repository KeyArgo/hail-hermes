"""Regressions for two user-visible failures found by auditing the live hub, not the code.

Both were invisible to 307 passing tests: the suite exercises branches, and these were product
outcomes — a flagship question answered with "On it." and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CONTENT = Path(__file__).resolve().parents[1] / "hail_hermes" / "content" / "souls.json"


def test_weather_is_answered_even_when_a_session_is_pinned():
    """A pinned project session must not silence the forecast.

    Observed on the owner's watch: with voice_session=pinned, "what's the weather today?" skipped the
    Open-Meteo fast path entirely, went to the agent, and came back as "On it." — no forecast at all.
    """
    source = (Path(__file__).resolve().parents[1] / "hail_hermes" / "hub.py").read_text(encoding="utf-8")
    assert 'saved.get("voice_session") == "pinned"' not in source, (
        "the weather path must not bail out because a voice session is pinned: a forecast is a device "
        "fact, not project work"
    )


@pytest.mark.parametrize("persona", json.loads(CONTENT.read_text(encoding="utf-8")), ids=lambda p: p.get("id", "?"))
def test_no_persona_answers_a_question_with_a_bare_handoff(persona):
    """"Say 'On it' and stop" as a standing instruction produces dead-end replies.

    Every persona may hand off a long task, but the instruction must also forbid using that line
    *instead of* answering something it can answer now.
    """
    soul = persona.get("soul") or ""
    assert "hand off plainly" not in soul, f"{persona.get('id')}: bare hand-off instruction is back"
    if "On it" in soul:
        assert "Never" in soul, (
            f"{persona.get('id')}: a hand-off line without a 'never use this as the answer' rule lets "
            "the model reply 'On it.' and stop"
        )
