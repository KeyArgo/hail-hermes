"""Turn queue ordering and cancellation."""

import asyncio

from hail_hermes import protocol as P
from conftest import Client, EchoTTS, FakeAgent, RecordingSTT, is_type, running_hub
from hail_hermes.quick import QuickAgent
from test_modes import FakeSSEServer, GatedAgent as ModesGatedAgent


class GatedAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def run(self, device, text):
        self.calls.append((device.device_id, text))
        try:
            if text == "first":
                yield "first draft"
                await self.release.wait()
            yield f"answer {text}"
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def sync_client(c):
    await c.send("ping", t=99)
    await c.until((lambda m: isinstance(m, dict) and m.get("type") == "pong" and m.get("t") == 99))


async def test_queue_order_and_oldest_waiter_dropped(state_dir):
    agent = GatedAgent()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="first")
        await c.until(is_type("transcript", role="assistant", text="first draft", final=False))
        for text in ("second", "third", "fourth", "fifth"):
            await c.send("text", text=text)
        await sync_client(c)
        assert [text for _, text in agent.calls] == ["first"]
        agent.release.set()
        msgs = await c.until(is_type("state", screen="idle"))
        assert [text for _, text in agent.calls] == ["first", "third", "fourth", "fifth"]
        finals = [m["text"] for m in msgs if is_type("transcript", role="assistant", final=True)(m)]
        assert finals == ["first draftanswer first", "answer third", "answer fourth", "answer fifth"]


async def test_audio_capture_queues_without_interrupting_current_turn(state_dir):
    agent = GatedAgent()
    # A one-word mumble is now ignored as a mis-hear, so the fixture speaks a sentence.
    stt = RecordingSTT("what time is it")
    async with running_hub(state_dir, agent=agent, stt=stt) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="first")
        await c.until(is_type("transcript", role="assistant", text="first draft", final=False))
        await c.send("audio_start", codec=P.CODEC, rate=P.SAMPLE_RATE)
        await c.send_raw(b"\x00" * (P.FRAME_BYTES * 20))
        await c.send("audio_end")
        await c.send("text", text="after voice")
        await sync_client(c)
        assert not agent.cancelled.is_set()
        assert stt.received == []
        agent.release.set()
        msgs = await c.until(is_type("state", screen="idle"))
        assert [text for _, text in agent.calls] == ["first", "what time is it", "after voice"]
        finals = [m["text"] for m in msgs if is_type("transcript", role="assistant", final=True)(m)]
        assert finals == ["first draftanswer first", "answer what time is it", "answer after voice"]


async def test_interrupt_clears_waiting_turns_and_capture(state_dir):
    agent = GatedAgent()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="first")
        await c.until(is_type("transcript", role="assistant", text="first draft", final=False))
        await c.send("text", text="second")
        await c.send("text", text="third")
        await c.send("audio_start", codec=P.CODEC, rate=P.SAMPLE_RATE)
        await c.send_raw(b"\x00" * (P.FRAME_BYTES * 20))
        await sync_client(c)
        await c.send("interrupt")
        await c.until(is_type("state", screen="idle"))
        await c.send("audio_end")
        await c.send("text", text="after interrupt")
        msgs = await c.until(is_type("state", screen="idle"))
        assert agent.cancelled.is_set()
        texts = [text for _, text in agent.calls]
        assert texts[0] == "first"
        # The turn after an interrupt carries the note so the agent knows what happened.
        assert texts[1].startswith("["), texts[1]
        assert texts[1].endswith("after interrupt"), texts[1]
        finals = [m["text"] for m in msgs if is_type("transcript", role="assistant", final=True)(m)]
        assert len(finals) == 1
        assert "after interrupt" in finals[0], finals


async def test_quick_history_persists_and_expires_per_device(state_dir, monkeypatch):
    import json
    import hail_hermes.quick as quick_module
    from hail_hermes.agent import DeviceInfo
    from hail_hermes.quick import QuickAgent

    now = [10000.0]
    monkeypatch.setattr(quick_module.time, "time", lambda: now[0])
    requests = []

    class Response:
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"answer"}}]}\n'
            yield b'data: [DONE]\n'

        def close(self):
            pass

    def open_response(request, timeout):
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(quick_module.urllib.request, "urlopen", open_response)
    kwargs = dict(soul_path=state_dir / "soul.md", state_dir=state_dir, max_history=1)
    first = QuickAgent("http://localhost", "test", **kwargs)
    d1, d2 = DeviceInfo("one"), DeviceInfo("two")
    assert [x async for x in first.run(d1, "first")] == ["answer"]
    assert [x async for x in first.run(d1, "second")] == ["answer"]
    now[0] += 100
    assert [x async for x in first.run(d2, "other")] == ["answer"]
    restored = QuickAgent("http://localhost", "test", **kwargs)
    assert restored.history("one") == [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "answer"},
    ]
    assert [x async for x in restored.run(d1, "follow up")] == ["answer"]
    assert requests[-1]["messages"][1:3] == first.history("one")
    now[0] += 1800
    assert [x async for x in restored.run(d1, "fresh")] == ["answer"]
    assert requests[-1]["messages"][1:] == [{"role": "user", "content": "fresh"}]
    assert restored.history("two") == []
    again = QuickAgent("http://localhost", "test", **kwargs)
    assert again.history("one")[0]["content"] == "fresh"
    assert again.history("two") == []
    again.clear("one")
    assert QuickAgent("http://localhost", "test", **kwargs).history("one") == []


async def test_rejected_toolsets_are_remembered_across_agents(monkeypatch):
    import hail_hermes.agent as agent_module
    from hail_hermes.agent import AgentError, DeviceInfo, HermesCliAgent

    monkeypatch.setattr(agent_module, "_REJECTED_TOOLSETS", set())
    calls = []

    class FakeHermes(HermesCliAgent):
        async def _run_once(self, device, text, **kwargs):
            argv = self.argv(device)
            calls.append(argv)
            if "-t" in argv:
                raise AgentError(detail="invalid_params")
            yield "ok"

    device = DeviceInfo("device")
    first = FakeHermes(binary="hermes", toolsets="clarify,pocket")
    second = FakeHermes(binary="hermes", toolsets="clarify,pocket")
    assert [x async for x in first.run(device, "one")] == ["ok"]
    assert [x async for x in second.run(device, "two")] == ["ok"]
    assert ["-t" in argv for argv in calls] == [True, False, False]


async def test_empty_stt_speaks_sorry_and_short_audio_reports_error(state_dir):
    from conftest import EchoTTS

    stt = RecordingSTT("")
    agent = FakeAgent()
    async with running_hub(state_dir, agent=agent, stt=stt, tts=EchoTTS(stt)) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("audio_start", codec=P.CODEC, rate=P.SAMPLE_RATE)
            await c.send_raw(b"\x00" * (P.FRAME_BYTES * 20))
            await c.send("audio_end")
            msgs = await c.until(is_type("state", screen="idle"))
            assert any(is_type("transcript", role="assistant", text="Sorry, I didn't catch that.", final=True)(m) for m in msgs)
            assert any(is_type("state", screen="thinking")(m) for m in msgs)
            assert any(is_type("state", screen="speaking")(m) for m in msgs)
            assert any(is_type("audio_start")(m) for m in msgs)
            assert any(isinstance(m, bytes) for m in msgs)
            assert agent.calls == []
            await c.send("audio_start", codec=P.CODEC, rate=P.SAMPLE_RATE)
            await c.send_raw(b"\x00" * P.FRAME_BYTES)
            await c.send("audio_end")
            msgs = await c.until(is_type("state", screen="idle"))
            assert any(is_type("error", code="too_short")(m) for m in msgs)
            assert len(stt.received) == 1
"""Hand-off delivery, progress, and Quick conversation context."""



async def test_handoff_progress_and_history(state_dir):
    full = ModesGatedAgent(chunks=("The answer.",))
    stt = RecordingSTT("hello")
    with FakeSSEServer([["[[HERMES]]"], ["Follow-up."]]) as sse:
        quick = QuickAgent(sse.url, "test")
        async with running_hub(
            state_dir, agent=full, stt=stt, tts=EchoTTS(stt), quick_agent=quick
        ) as hub:
            async with Client.open(hub) as c:
                device_id, _, _ = await c.pair(modes=True)
                await c.send("text", text="A question")
                messages = await c.until(is_type("state", screen="idle"))
                # "working" must arrive before the watch sees idle.
                assert [
                    m["count"] for m in messages
                    if isinstance(m, dict) and m.get("type") == "working"
                ] == [1]
                full.release.set()
                messages = await c.until(is_type("state", screen="idle"))
                assert [
                    m["count"] for m in messages
                    if isinstance(m, dict) and m.get("type") == "working"
                ] == [0]
                assert any(
                    is_type("transcript", role="assistant",
                            text="The answer.", final=True)(m)
                    for m in messages
                )
                assert quick.history(device_id)[-2:] == [
                    {"role": "user", "content": "A question"},
                    {"role": "assistant", "content": "The answer."},
                ]
                await c.send("text", text="Follow up")
                await c.until(is_type("state", screen="idle"))
                assert sse.requests[-1]["body"]["messages"][-3:-1] == [
                    {"role": "user", "content": "A question"},
                    {"role": "assistant", "content": "The answer."},
                ]


async def test_handoff_offline_delivered_after_welcome(state_dir):
    full = ModesGatedAgent(chunks=("Returned answer.",))
    stt = RecordingSTT("hello")
    with FakeSSEServer([["[[HERMES]]"]]) as sse:
        quick = QuickAgent(sse.url, "test")
        async with running_hub(
            state_dir, agent=full, stt=stt, tts=EchoTTS(stt), quick_agent=quick
        ) as hub:
            async with Client.open(hub) as c:
                device_id, token, _ = await c.pair()
                await c.send("text", text="Question")
                await c.until(is_type("state", screen="idle"))
                await asyncio.wait_for(full.started.wait(), 2)
            full.release.set()
            await asyncio.wait_for(asyncio.gather(*list(hub._handoff_tasks)), 2)
            async with Client.open(hub) as c:
                await c.hello(device_id, token)
                await c.expect("welcome")
                messages = await c.until(is_type("state", screen="idle"))
                assert any(
                    is_type("transcript", role="assistant",
                            text="Returned answer.", final=True)(m)
                    for m in messages
                )


async def test_handoff_waits_behind_busy_turn(state_dir):
    full = ModesGatedAgent(chunks=("Background answer.",))
    gate = asyncio.Event()
    started = asyncio.Event()

    class Quick:
        async def run(self, device, text):
            if text == "First":
                yield "[[HERMES]]"
            else:
                started.set()
                await gate.wait()
                yield "Foreground answer."

    async with running_hub(state_dir, agent=full, quick_agent=Quick()) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("text", text="First")
            await c.until(is_type("state", screen="idle"))
            await c.send("text", text="Second")
            await asyncio.wait_for(started.wait(), 2)
            full.release.set()
            await asyncio.wait_for(asyncio.gather(*list(hub._handoff_tasks)), 2)
            gate.set()
            messages = await c.until(is_type("state", screen="idle"))
            finals = [
                m["text"] for m in messages
                if isinstance(m, dict) and m.get("type") == "transcript"
                and m.get("role") == "assistant" and m.get("final")
            ]
            assert finals == ["Foreground answer.", "Background answer."]
