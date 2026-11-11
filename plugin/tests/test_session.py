"""Text roundtrip, audio echo, state transitions, interrupt, error paths."""

import asyncio

from hail_hermes import protocol as P

from conftest import Client, EchoTTS, FakeAgent, RecordingSTT, is_type, running_hub, screens


async def test_text_roundtrip_streams_transcripts_and_states(state_dir):
    agent = FakeAgent(["Hello ", "from ", "Hermes."])
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        device_id, _, _ = await c.pair()
        await c.send("text", text="hi there")
        msgs = await c.until(is_type("state", screen="idle"))
        assert msgs[0] == {"type": "transcript", "v": 2, "role": "user", "text": "hi there", "final": True}
        assert screens(msgs) == ["thinking", "idle"]
        assistant = [m for m in msgs if m["type"] == "transcript" and m["role"] == "assistant"]
        assert [m["text"] for m in assistant if not m["final"]] == ["Hello ", "from ", "Hermes."]
        finals = [m for m in assistant if m["final"]]
        assert len(finals) == 1 and finals[0]["text"] == "Hello from Hermes."
        assert assistant[-1]["final"]  # final=true exactly once, last
        assert agent.calls == [(device_id, "hi there")]


async def test_text_roundtrip_with_tts_goes_through_speaking(state_dir):
    stt = RecordingSTT()
    async with running_hub(state_dir, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="speak to me")
        msgs = await c.until(is_type("state", screen="idle"))
        assert screens(msgs) == ["thinking", "speaking", "idle"]
        i_start = next(i for i, m in enumerate(msgs) if is_type("audio_start")(m))
        i_end = next(i for i, m in enumerate(msgs) if is_type("audio_end")(m))
        assert msgs[i_start] == {"type": "audio_start", "v": 2, "codec": "pcm16le", "rate": 16000}
        audio = msgs[i_start + 1:i_end]
        assert audio and all(isinstance(f, bytes) and len(f) == P.FRAME_BYTES for f in audio)
        # Final transcript precedes the audio; state=speaking precedes audio_start.
        i_final = next(i for i, m in enumerate(msgs) if is_type("transcript", role="assistant", final=True)(m))
        i_speaking = next(i for i, m in enumerate(msgs) if is_type("state", screen="speaking")(m))
        assert i_final < i_speaking < i_start < i_end


async def test_audio_echo(state_dir):
    """Mic PCM → STT → agent → TTS that echoes the mic audio back, frame-exact."""
    stt = RecordingSTT()
    agent = FakeAgent(["ok"])
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair(kind="wear")
        frames = [bytes([i]) * P.FRAME_BYTES for i in range(1, 16)]  # 15 x 20 ms
        await c.send("audio_start", codec="pcm16le", rate=16000)
        assert (await c.expect("state"))["screen"] == "listening"
        for f in frames:
            await c.send_raw(f)
        await c.send("audio_end")
        msgs = await c.until(is_type("state", screen="idle"))
        assert stt.received == [b"".join(frames)]
        assert screens(msgs) == ["thinking", "speaking", "idle"]
        user = next(m for m in msgs if is_type("transcript", role="user")(m))
        assert user["text"] == "heard 9600 bytes" and user["final"]
        assert agent.calls[0][1] == "heard 9600 bytes"
        played = [m for m in msgs if isinstance(m, bytes)]
        assert played == frames


async def test_partial_tts_frame_is_padded(state_dir):
    class OddTTS:
        async def synthesize(self, text):
            return b"\x01\x02" * 500  # 1000 bytes -> 640 + 360 padded

    async with running_hub(state_dir, tts=OddTTS()) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="x")
        msgs = await c.until(is_type("audio_end"))
        played = [m for m in msgs if isinstance(m, bytes)]
        assert [len(f) for f in played] == [640, 640]
        assert played[1][360:] == b"\x00" * 280


async def test_empty_stt_result_returns_to_idle(state_dir):
    agent = FakeAgent()
    async with running_hub(state_dir, agent=agent, stt=RecordingSTT("")) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("audio_start", codec="pcm16le", rate=16000)
        await c.expect("state")
        await c.send_raw(b"\x00" * P.FRAME_BYTES * 15)
        await c.send("audio_end")
        msgs = await c.until(is_type("state", screen="idle"))
        assert not agent.calls
        assert screens(msgs)[-1] == "idle"


async def test_audio_protocol_errors(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send_raw(b"\x00" * P.FRAME_BYTES)
        assert (await c.expect("error"))["code"] == "bad_frame"
        await c.send("audio_end")
        # Ending an utterance that never started is harmless.
        assert await c.silent()
        await c.send("audio_start", codec="opus", rate=48000)
        assert (await c.expect("error"))["code"] == "bad_frame"
        assert await c.silent()


async def test_audio_too_long_is_dropped(state_dir):
    async with running_hub(state_dir, max_audio_seconds=0.05) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("audio_start", codec="pcm16le", rate=16000)
        await c.expect("state")
        for _ in range(3):  # 60 ms > 50 ms
            await c.send_raw(b"\x00" * P.FRAME_BYTES)
        assert (await c.expect("error"))["code"] == "too_long"
        assert (await c.expect("state"))["screen"] == "idle"


async def test_interrupt_cancels_agent_and_returns_idle(state_dir):
    agent = FakeAgent(["thinking..."], block=True)
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="long task")
        await c.until(is_type("transcript", role="assistant", final=False))
        await c.send("interrupt")
        msgs = await c.until(is_type("state", screen="idle"))
        assert agent.cancelled.is_set()
        assert not any(is_type("transcript", role="assistant", final=True)(m) for m in msgs)
        assert await c.silent()


async def test_interrupt_during_speaking_stops_audio(state_dir):
    class SlowTTS:
        async def synthesize(self, text):
            return b"\x00" * P.FRAME_BYTES * 200  # 4 s, paced

    async with running_hub(state_dir, tts=SlowTTS(), audio_pacing=True) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="talk a lot")
        await c.until(is_type("audio_start"))
        await c.send("interrupt")
        msgs = await c.until(is_type("state", screen="idle"))
        assert sum(isinstance(m, bytes) for m in msgs) < 200
        assert await c.silent()


async def test_new_text_waits_for_current_turn(state_dir):
    class GatedAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()

        async def run(self, device, text):
            self.calls.append((device.device_id, text))
            yield text
            if text == "first":
                await self.release.wait()

    agent = GatedAgent()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="first")
        await c.until(is_type("transcript", role="assistant"))
        await c.send("text", text="second")
        await c.send("ping", t=1)
        await c.until(lambda m: isinstance(m, dict) and m.get("type") == "pong" and m.get("t") == 1)
        assert [t for _, t in agent.calls] == ["first"]
        agent.release.set()
        msgs = await c.until(is_type("transcript", role="assistant", text="second", final=True))
        assert [t for _, t in agent.calls] == ["first", "second"]
        assert [m["text"] for m in msgs if is_type("transcript", role="assistant", final=True)(m)] == ["first", "second"]


async def test_agent_failure_reports_safe_error(state_dir):
    async with running_hub(state_dir, agent=FakeAgent(fail="model offline")) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="hi")
        msgs = await c.until(is_type("state", screen="idle"))
        err = next(m for m in msgs if is_type("error")(m))
        assert err["code"] == "agent_failed"
        assert "model offline" not in err["message"]


async def test_ping_unknown_and_invalid_messages(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("ping", t=12345)
        assert (await c.expect("pong"))["t"] == 12345
        await c.send("from_the_future", x=1)  # unknown types are ignored
        assert await c.silent()
        await c.send_raw("[1,2]")
        assert (await c.expect("error"))["code"] == "internal"
        await c.send("text", text="   ")
        assert (await c.expect("error"))["code"] == "internal"
        await c.send("hello", device_id="x", kind="sim")
        assert (await c.expect("error"))["code"] == "internal"


async def test_state_is_only_pushed_on_change(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("interrupt")  # idle -> idle: no state message
        assert await c.silent()


async def test_two_devices_are_independent(state_dir):
    agent = FakeAgent(["pong"])
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as a, Client.open(hub) as b:
        id_a, _, _ = await a.pair(kind="phone")
        id_b, _, _ = await b.pair(kind="wear")
        await a.send("text", text="from a")
        await a.until(is_type("state", screen="idle"))
        assert await b.silent()
        await b.send("text", text="from b")
        await b.until(is_type("state", screen="idle"))
        assert agent.calls == [(id_a, "from a"), (id_b, "from b")]
