"""Sentence-streaming TTS speed, ordering, and interrupt tests."""

from __future__ import annotations

import asyncio
import time

from hail_hermes import protocol as P
from hail_hermes.control import ControlClient
from hail_hermes.hub import _split_sentences

from conftest import Client, is_type, running_hub

REPLY_TEXT = "Hello there. This is the second sentence. And a third."


class SlowDeltaAgent:
    """Yields ``text`` in small slices with sleeps so TTS can overlap generation."""

    def __init__(self, text: str = REPLY_TEXT, step: int = 6, delay: float = 0.02):
        self.text = text
        self.step = step
        self.delay = delay
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        self.cancelled = asyncio.Event()

    async def run(self, device, text):
        self.started_at = time.monotonic()
        try:
            for i in range(0, len(self.text), self.step):
                await asyncio.sleep(self.delay)
                yield self.text[i : i + self.step]
            await asyncio.sleep(self.delay)
            self.finished_at = time.monotonic()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class RecordingTTS:
    """Records call order, timing, and returns distinct tagged PCM frames per chunk."""

    def __init__(self, delay: float = 0.02, frames_per_call: int = 3):
        self.delay = delay
        self.frames_per_call = frames_per_call
        self.calls: list[str] = []
        self.call_times: list[float] = []
        self.first_started = asyncio.Event()

    async def synthesize(self, text: str) -> bytes:
        idx = len(self.calls) + 1
        self.calls.append(text)
        self.call_times.append(time.monotonic())
        self.first_started.set()
        await asyncio.sleep(self.delay)
        tag = bytes([idx, 0]) * (P.FRAME_BYTES // 2)
        return tag * self.frames_per_call


def test_split_sentences_rules():
    # Short first sentence (<20 chars) merges with the second; abbreviations and decimals never split.
    chunks, rem = _split_sentences("Hello there. This is the second sentence. And a third.", final=False)
    assert chunks == ["Hello there. This is the second sentence."]
    assert rem == "And a third."
    tail, rem2 = _split_sentences(rem, final=True)
    assert tail == ["And a third."] and rem2 == ""

    chunks, rem = _split_sentences("Value is 3.5, e.g. a float! Next part", final=False)
    assert chunks == ["Value is 3.5, e.g. a float!"]
    assert rem == "Next part"

    chunks, rem = _split_sentences("Line one is over twenty chars\nTail", final=False)
    assert chunks == ["Line one is over twenty chars"]
    assert rem == "Tail"


async def test_stream_speech_starts_before_agent_finishes_and_preserves_order(state_dir):
    agent = SlowDeltaAgent()
    tts = RecordingTTS(delay=0.01, frames_per_call=2)
    async with running_hub(state_dir, agent=agent, tts=tts, stream_speech=True) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("text", text="go")
            msgs = await c.until(is_type("state", screen="idle"))
            assert await c.silent(0.1)

    assert len(tts.calls) == 2
    assert tts.calls == ["Hello there. This is the second sentence.", "And a third."]
    assert tts.call_times[0] < agent.finished_at, "sentence 1 synthesis must start before agent finishes"

    starts = [i for i, m in enumerate(msgs) if is_type("audio_start")(m)]
    ends = [i for i, m in enumerate(msgs) if is_type("audio_end")(m)]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]

    frames = [m for m in msgs[starts[0] + 1 : ends[0]] if isinstance(m, bytes)]
    assert len(frames) == 4
    assert [f[0] for f in frames] == [1, 1, 2, 2]

    assistant_msgs = [m for m in msgs if is_type("transcript", role="assistant")(m)]
    deltas = [m["text"] for m in assistant_msgs if not m["final"]]
    finals = [m["text"] for m in assistant_msgs if m["final"]]
    assert "".join(deltas) == REPLY_TEXT
    assert finals == [REPLY_TEXT]


async def test_interrupt_after_first_chunk_sends_no_further_audio(state_dir):
    agent = SlowDeltaAgent(delay=0.03)
    tts = RecordingTTS(delay=0.01, frames_per_call=2)
    async with running_hub(state_dir, agent=agent, tts=tts, stream_speech=True) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("text", text="interrupt me")
            # Wait until the first binary audio frame of chunk 1 arrives, then interrupt.
            await c.until(lambda m: isinstance(m, bytes))
            await c.send("interrupt")
            post = await c.until(is_type("state", screen="idle"))
            assert await c.silent(0.2)

    # No chunk-2 frames (tagged with byte 2) may arrive after interrupt.
    assert not any(isinstance(m, bytes) and m[0] == 2 for m in post)
    assert tts.calls == ["Hello there. This is the second sentence."]


async def test_stream_speech_disabled_waits_for_full_reply(state_dir):
    agent = SlowDeltaAgent(delay=0.01)
    tts = RecordingTTS(delay=0.01, frames_per_call=2)
    async with running_hub(state_dir, agent=agent, tts=tts, stream_speech=False) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("text", text="no stream")
            msgs = await c.until(is_type("state", screen="idle"))
            assert await c.silent(0.1)

    assert tts.calls == [REPLY_TEXT]
    assert tts.call_times[0] >= agent.finished_at
    starts = [i for i, m in enumerate(msgs) if is_type("audio_start")(m)]
    ends = [i for i, m in enumerate(msgs) if is_type("audio_end")(m)]
    assert len(starts) == 1 and len(ends) == 1


async def test_device_say_ordering_before_and_after_reply_playback_starts(state_dir):
    class _LateSayAgent:
        def __init__(self, early: bool):
            self.early = early

        async def run(self, device, text):
            if self.early:
                await asyncio.to_thread(
                    ControlClient(state_dir).call,
                    "say",
                    device_id=device.device_id,
                    text="Say utterance.",
                )
            yield "First sentence is definitely long enough. "
            if not self.early:
                await asyncio.sleep(0.05)
                await asyncio.to_thread(
                    ControlClient(state_dir).call,
                    "say",
                    device_id=device.device_id,
                    text="Say utterance.",
                )
            yield "Second sentence finishes."

    agent = _LateSayAgent(early=True)
    tts = RecordingTTS(delay=0.01, frames_per_call=1)
    async with running_hub(state_dir, agent=agent, tts=tts, stream_speech=True) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("text", text="early say")
            await c.until(is_type("state", screen="idle"))
            assert tts.calls[0] == "Say utterance."
            assert tts.calls[1:] == [
                "First sentence is definitely long enough.",
                "Second sentence finishes.",
            ]

            tts.calls.clear()
            agent.early = False
            await c.send("text", text="late say")
            await c.until(is_type("state", screen="idle"))
            assert tts.calls == [
                "First sentence is definitely long enough.",
                "Second sentence finishes.",
                "Say utterance.",
            ]
