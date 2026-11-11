"""Turn lifecycle reliability tests: generation counter, audio gating, reconnect, and TTS envelopes."""

from __future__ import annotations

import asyncio
import json

from hail_hermes import protocol as P
from hail_hermes.control import ControlClient

from conftest import Client, EchoTTS, FakeAgent, RecordingSTT, is_type, running_hub, screens


class _LateOutputAgent(FakeAgent):
    """FakeAgent that attempts to emit stale output after cancellation on odd-numbered turns."""

    def __init__(self):
        super().__init__(chunks=("new-reply",), block=False)
        self.turn_index = 0

    async def run(self, device, text):
        self.turn_index += 1
        idx = self.turn_index
        self.calls.append((device.device_id, text))
        self.started.set()
        if idx in (1, 3):
            try:
                yield f"turn-{idx}-delta "
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                yield f"stale-from-turn-{idx}"
                return
        elif idx == 2:
            for chunk in self.chunks:
                yield chunk
        else:
            try:
                yield "disconnect-delta "
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise


class _SayInTurnAgent(FakeAgent):
    """FakeAgent that invokes device_say through the control channel mid-turn."""

    def __init__(self, state_dir, say_text: str, chunks: tuple[str, ...]):
        super().__init__(chunks=chunks)
        self.state_dir = state_dir
        self.say_text = say_text

    async def run(self, device, text):
        self.calls.append((device.device_id, text))
        self.started.set()
        await asyncio.to_thread(
            ControlClient(self.state_dir).call,
            "say",
            device_id=device.device_id,
            text=self.say_text,
        )
        for chunk in self.chunks:
            yield chunk


class _HungAfterCancelAgent(FakeAgent):
    """The first and third turns ignore cancellation until explicitly released."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.cancel_count = 0

    async def run(self, device, text):
        self.calls.append((device.device_id, text))
        if text in ("first", "third"):
            try:
                yield "old draft"
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_count += 1
                await self.release.wait()
                yield "stale output"
        else:
            yield "new reply"


class _RepeatedSayAgent(FakeAgent):
    def __init__(self, state_dir, reply):
        super().__init__()
        self.state_dir = state_dir
        self.reply = reply

    async def run(self, device, text):
        self.calls.append((device.device_id, text))
        for _ in range(2):
            await asyncio.to_thread(
                ControlClient(self.state_dir).call,
                "say",
                device_id=device.device_id,
                text="hello",
            )
        if self.reply:
            yield self.reply


class _StreamingSayAgent(FakeAgent):
    def __init__(self, state_dir):
        super().__init__()
        self.state_dir = state_dir

    async def run(self, device, text):
        self.calls.append((device.device_id, text))
        yield "Final "
        await asyncio.to_thread(
            ControlClient(self.state_dir).call,
            "say",
            device_id=device.device_id,
            text="Heads up.",
        )
        yield "reply."


class GatedEmptySTT(RecordingSTT):
    """STT that blocks until released, then returns empty text."""

    def __init__(self):
        super().__init__("   ")
        self.release = asyncio.Event()

    async def transcribe(self, pcm: bytes) -> str:
        self.received.append(pcm)
        await self.release.wait()
        return "   "


async def control_say(state_dir, **args) -> dict:
    from hail_hermes.tools import make_handler

    raw = await asyncio.to_thread(
        make_handler("say", ControlClient(state_dir)),
        dict(args),
        task_id="t1",
    )
    return json.loads(raw)


def _count_non_interleaved_envelopes(msgs: list) -> int:
    """Verify audio_start/audio_end envelopes never interleave and return the envelope count."""
    in_audio = False
    frames_in_current = 0
    envelopes = 0
    for msg in msgs:
        if isinstance(msg, bytes):
            assert in_audio, "received binary audio frame outside audio_start/audio_end"
            assert len(msg) == P.FRAME_BYTES
            frames_in_current += 1
        elif is_type("audio_start")(msg):
            assert not in_audio, "nested/interleaved audio_start"
            in_audio = True
            frames_in_current = 0
        elif is_type("audio_end")(msg):
            assert in_audio, "audio_end without matching audio_start"
            assert frames_in_current > 0, "empty audio envelope"
            in_audio = False
            envelopes += 1
    assert not in_audio, "unclosed audio_start envelope"
    return envelopes


async def test_generation_counter_suppresses_old_turn_output(state_dir):
    stt = RecordingSTT()
    agent = _LateOutputAgent()
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub:
        async with Client.open(hub) as c:
            await c.pair()

            # 1) Explicit interruption invalidates late output before a new turn.
            await c.send("text", text="first")
            await c.until(is_type("transcript", role="assistant", text="turn-1-delta ", final=False))
            await c.send("interrupt")
            await c.until(is_type("state", screen="idle"))
            await c.send("text", text="second")
            msgs = await c.until(is_type("state", screen="idle"))
            assert await c.silent(0.2)
            assert agent.cancelled.is_set()
            assert not any(
                isinstance(m, dict) and "stale-from-turn-1" in m.get("text", "")
                for m in msgs
            )
            finals = [m for m in msgs if is_type("transcript", role="assistant", final=True)(m)]
            assert len(finals) == 1 and finals[0]["text"] == "new-reply"
            assert _count_non_interleaved_envelopes(msgs) == 1
            # Turn 1 must not push a stale state="idle" before turn 2 finishes speaking.
            assert not any(is_type("state", screen="idle")(m) for m in msgs[:-1])

            # 2) Interrupt cancels an in-flight turn: no late delta, final, or TTS after interrupt.
            agent.cancelled.clear()
            await c.send("text", text="third")
            await c.until(is_type("transcript", role="assistant", text="turn-3-delta ", final=False))
            await c.send("interrupt")
            int_msgs = await c.until(is_type("state", screen="idle"))
            assert await c.silent(0.2)
            assert agent.cancelled.is_set()
            assert not any(
                isinstance(m, dict) and "stale-from-turn-3" in m.get("text", "")
                for m in int_msgs
            )
            assert not any(is_type("transcript", role="assistant", final=True)(m) for m in int_msgs)
            assert not any(is_type("audio_start")(m) or isinstance(m, bytes) for m in int_msgs)

            # 3) A dropped connection no longer cancels the turn: it keeps running so its
            #    reply can reach the next connection (owner, 2026-11-07: never lose the question).
            agent.cancelled.clear()
            await c.send("text", text="fourth")
            await c.until(is_type("transcript", role="assistant", text="disconnect-delta ", final=False))
        await asyncio.sleep(0.3)
        assert not agent.cancelled.is_set()


async def test_hung_cancelled_turn_does_not_block_text_or_interrupt(state_dir):
    agent = _HungAfterCancelAgent()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await c.pair()
        try:
            await c.send("text", text="first")
            await c.until(is_type("transcript", role="assistant", text="old draft", final=False))
            await c.send("interrupt")
            await c.until(is_type("state", screen="idle"), timeout=1.0)
            await c.send("text", text="second")
            msgs = await c.until(is_type("state", screen="idle"), timeout=1.0)
            assert any(is_type("transcript", role="assistant", text="new reply", final=True)(m) for m in msgs)
            assert agent.cancel_count == 1

            await c.send("text", text="third")
            await c.until(is_type("transcript", role="assistant", text="old draft", final=False))
            await c.send("interrupt")
            await c.until(is_type("state", screen="idle"), timeout=1.0)
            await c.send("ping", t=42)
            await c.until((lambda m: isinstance(m, dict) and m["type"] == "pong" and m.get("t") == 42), timeout=1.0)
            assert agent.cancel_count == 2

            agent.release.set()
            assert await c.silent(0.2)
        finally:
            agent.release.set()


async def test_double_audio_end_and_text_waits_for_audio_turn(state_dir):
    class GatedAgent(FakeAgent):
        def __init__(self):
            super().__init__(["voice answer"])
            self.release = asyncio.Event()

        async def run(self, device, text):
            self.calls.append((device.device_id, text))
            if text == "voice query" and len(self.calls) == 1:
                yield "voice answer"
            elif text == "voice query":
                yield "audio partial "
                await self.release.wait()
            else:
                yield "typed answer"

    stt = RecordingSTT("voice query")
    agent = GatedAgent()
    async with running_hub(state_dir, agent=agent, stt=stt) as hub, Client.open(hub) as c:
        await c.pair()

        # Two audio_end frames in a row -> exactly one agent run and no error.
        await c.send("audio_start", codec="pcm16le", rate=16000)
        assert (await c.expect("state"))["screen"] == "listening"
        for _ in range(20):  # 400 ms >= 300 ms
            await c.send_raw(b"\x01\x00" * (P.FRAME_BYTES // 2))
        await c.send("audio_end")
        await c.send("audio_end")
        msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)
        assert not any(is_type("error")(m) for m in msgs)
        assert len(agent.calls) == 1 and agent.calls[0][1] == "voice query"

        # A text message during an audio turn waits for its answer.
        await c.send("audio_start", codec="pcm16le", rate=16000)
        assert (await c.expect("state"))["screen"] == "listening"
        for _ in range(20):
            await c.send_raw(b"\x02\x00" * (P.FRAME_BYTES // 2))
        await c.send("audio_end")
        await c.until(is_type("transcript", role="assistant", text="audio partial ", final=False))

        await c.send("text", text="text follow-up")
        await c.send("ping", t=1)
        await c.until(lambda m: isinstance(m, dict) and m.get("type") == "pong" and m.get("t") == 1)
        assert [t for _, t in agent.calls] == ["voice query", "voice query"]
        agent.release.set()
        msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)
        finals = [m["text"] for m in msgs if is_type("transcript", role="assistant", final=True)(m)]
        assert finals == ["audio partial", "typed answer"]
        assert [t for _, t in agent.calls] == ["voice query", "voice query", "text follow-up"]


async def test_unstarted_or_short_audio_utterance_returns_idle_without_agent_run(state_dir):
    stt = RecordingSTT("should not run")
    agent = FakeAgent(["nope"])
    async with running_hub(state_dir, agent=agent, stt=stt) as hub, Client.open(hub) as c:
        await c.pair()

        # audio_end with no prior audio_start -> no error, no agent run, stays idle.
        await c.send("audio_end")
        assert await c.silent(0.2)
        assert agent.calls == [] and stt.received == []

        # Utterance < 300 ms (5 frames = 100 ms) -> error too_short (watch shows "Hold to talk"), no agent run, idle.
        await c.send("audio_start", codec="pcm16le", rate=16000)
        assert (await c.expect("state"))["screen"] == "listening"
        for _ in range(5):
            await c.send_raw(b"\x00" * P.FRAME_BYTES)
        await c.send("audio_end")
        msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)
        assert [m["code"] for m in msgs if is_type("error")(m)] == ["too_short"]
        assert agent.calls == [] and stt.received == []


async def test_reconnect_mid_turn_keeps_old_turn_and_new_conn_hears_nothing_stale(state_dir):
    stt = RecordingSTT()
    agent = _LateOutputAgent()
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub:
        async with Client.open(hub) as old:
            device_id, token, _ = await old.pair()
            await old.send("text", text="slow turn")
            await old.until(is_type("transcript", role="assistant", text="turn-1-delta ", final=False))

            async with Client.open(hub) as new:
                await new.hello(device_id, token=token)
                welcome = await new.expect("welcome")
                assert welcome["device_id"] == device_id and welcome["state"] == "idle"
                # The old turn is still waiting on its agent; nothing stale reaches the new connection.
                assert await new.silent(0.3)
                assert not agent.cancelled.is_set()
                assert hub.sessions[device_id].screen == "idle"


async def test_device_say_during_turn_never_interleaves_and_deduplicates_identical_text(state_dir):
    stt = RecordingSTT()
    agent = _SayInTurnAgent(state_dir, say_text="Heads up.", chunks=("Final ", "reply."))
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()

        # Distinct device_say text and assistant reply -> two separate non-interleaved envelopes.
        await c.send("text", text="run tool")
        msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)
        assert _count_non_interleaved_envelopes(msgs) == 2

        # Identical device_say text and assistant reply -> spoken only once (single envelope).
        agent.say_text = "Same text."
        agent.chunks = ["Same ", "text."]
        await c.send("text", text="run duplicate")
        dup_msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)
        assert _count_non_interleaved_envelopes(dup_msgs) == 1


async def test_repeated_identical_device_say_calls_are_each_spoken(state_dir):
    stt = RecordingSTT()
    agent = _RepeatedSayAgent(state_dir, reply="different reply")
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="repeat")
        msgs = await c.until(is_type("state", screen="idle"))
        assert _count_non_interleaved_envelopes(msgs) == 3

        # Two identical calls remain two envelopes even when the reply matches.
        agent.reply = "hello"
        await c.send("text", text="repeat with matching reply")
        msgs = await c.until(is_type("state", screen="idle"))
        assert _count_non_interleaved_envelopes(msgs) == 2


async def test_streamed_reply_has_one_matching_final_with_distinct_device_say(state_dir):
    stt = RecordingSTT()
    agent = _StreamingSayAgent(state_dir)
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="stream and speak")
        msgs = await c.until(is_type("state", screen="idle"))
        transcripts = [m for m in msgs if is_type("transcript", role="assistant")(m)]
        deltas = [m["text"] for m in transcripts if not m["final"]]
        finals = [m["text"] for m in transcripts if m["final"]]
        assert "".join(deltas) == "Final reply."
        assert finals == ["Final reply."]
        assert _count_non_interleaved_envelopes(msgs) == 2


async def test_one_spoken_reply_has_single_audio_envelope_and_idle_after_audio_end(state_dir):
    stt = RecordingSTT()
    agent = FakeAgent(["Part one. ", "Part two. ", "Part three."])
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="speak once")
        msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)

        starts = [i for i, m in enumerate(msgs) if is_type("audio_start")(m)]
        ends = [i for i, m in enumerate(msgs) if is_type("audio_end")(m)]
        idles = [i for i, m in enumerate(msgs) if is_type("state", screen="idle")(m)]
        assert len(starts) == 1 and len(ends) == 1 and len(idles) == 1
        assert starts[0] < ends[0] < idles[0]
        assert _count_non_interleaved_envelopes(msgs) == 1
        assert screens(msgs) == ["thinking", "speaking", "idle"]


async def test_empty_stt_returns_to_idle_without_agent_run_or_error(state_dir):
    stt = RecordingSTT("   ")
    agent = FakeAgent(["unreachable"])
    async with running_hub(state_dir, agent=agent, stt=stt) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("audio_start", codec="pcm16le", rate=16000)
        assert (await c.expect("state"))["screen"] == "listening"
        for _ in range(20):  # 400 ms >= 300 ms
            await c.send_raw(b"\x00" * P.FRAME_BYTES)
        await c.send("audio_end")
        msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)
        assert len(stt.received) == 1
        assert agent.calls == []
        assert not any(is_type("error")(m) for m in msgs)
        # Never silent: the hub says it didn't catch that (no agent run).
        assert [m["text"] for m in msgs if is_type("transcript", role="assistant", final=True)(m)] == [
            "Sorry, I didn't catch that."]
        assert screens(msgs)[-1] == "idle"


async def test_device_say_during_empty_stt_turn_is_drained(state_dir):
    """Queued device_say while STT runs must still be spoken when STT returns empty."""
    stt = GatedEmptySTT()
    agent = FakeAgent(["unreachable"])
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("audio_start", codec="pcm16le", rate=16000)
        assert (await c.expect("state"))["screen"] == "listening"
        for _ in range(20):
            await c.send_raw(b"\x00" * P.FRAME_BYTES)
        await c.send("audio_end")
        assert (await c.expect("state"))["screen"] == "thinking"
        res = await control_say(state_dir, text="Alert during STT.")
        assert res["ok"] and res.get("queued")
        stt.release.set()
        msgs = await c.until(is_type("state", screen="idle"))
        assert await c.silent(0.2)
        assert agent.calls == []
        finals = [m["text"] for m in msgs if is_type("transcript", role="assistant", final=True)(m)]
        assert "Alert during STT." in " ".join(finals) and "Sorry, I didn't catch that." in " ".join(finals)
        # The queued alert and the sorry line are two spoken replies, one after the other (never interleaved).
        assert _count_non_interleaved_envelopes(msgs) == 2


async def test_control_say_after_turn_finishes_not_deferred_to_next_text(state_dir):
    """Control say after reply audio must not sit in pending_say for a later text turn."""
    stt = RecordingSTT()
    agent = FakeAgent(["Answer."])
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="one")
        await c.until(is_type("audio_end"))
        res = await control_say(state_dir, text="Follow up.")
        assert res["ok"] and not res.get("queued")
        # The turn's own idle may arrive first; the line must still arrive now, with no later turn to carry it.
        await c.until(is_type("transcript", role="assistant", text="Follow up.", final=True))
        await c.until(is_type("state", screen="idle"))
        agent.calls.clear()
        await c.send("text", text="two")
        second = await c.until(is_type("state", screen="idle"))
        assert not any(
            is_type("transcript", role="assistant", text="Follow up.", final=True)(m)
            for m in second
        )
