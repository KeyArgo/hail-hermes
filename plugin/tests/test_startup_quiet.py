"""A hub with replies turned off starts quietly.

Bug found on a fresh profile: `serve --tts none` logged two tracebacks at start-up, one per brief sentence,
because the background pre-warm called `_synthesize` and hit `assert self.tts is not None`. Nothing was
broken (the hub served fine), but a log a user is asked to paste looked like a crash on every single start —
and an `assert` disappears under `python -O`, so the same code would behave differently there.

These start the real hub with `warm_voice=True`, let the pre-warm task finish, and read the log.
"""

from __future__ import annotations

import logging

from conftest import EchoTTS, RecordingSTT, running_hub


async def test_no_speech_means_no_tracebacks(state_dir, caplog):
    caplog.set_level(logging.DEBUG)
    async with running_hub(state_dir, tts=None, warm_voice=True) as hub:
        task = getattr(hub, "_brief_warm_task", None)
        if task is not None:
            await task

    noisy = [r for r in caplog.records if r.exc_info or "AssertionError" in r.getMessage()]
    assert not noisy, [(r.levelname, r.getMessage()) for r in noisy]


async def test_no_speech_means_no_synthesis_attempt(state_dir, monkeypatch):
    """The guard is real work skipped, not a try/except swallowing a failure."""
    calls: list[str] = []

    async def record(text, voice=None):
        calls.append(text)
        return b"\x00\x00" * 10

    async with running_hub(state_dir, tts=None, warm_voice=True) as hub:
        monkeypatch.setattr(hub, "_synthesize_remembered", record)
        await hub._warm_brief_audio()

    assert calls == [], "a hub without a speech backend tried to synthesise"


async def test_with_speech_the_pre_warm_still_runs(state_dir, monkeypatch):
    """The other half: the guard must not skip the warm-up when there *is* a backend."""
    calls: list[str] = []

    async def record(text, voice=None):
        calls.append(text)
        return b"\x00\x00" * 10

    stt = RecordingSTT("hello")
    async with running_hub(state_dir, stt=stt, tts=EchoTTS(stt), warm_voice=True) as hub:
        monkeypatch.setattr(hub, "_synthesize_remembered", record)
        await hub._warm_brief_audio()

    assert calls, "with speech available the brief should still be warmed"
