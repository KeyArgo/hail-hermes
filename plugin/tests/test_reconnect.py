"""Reconnect handling: token re-hello, replacement of stale connections, idle timeout, live snapshot."""

import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosed

from conftest import Client, FakeAgent, is_type, running_hub


async def test_reconnect_resets_state_to_idle(state_dir):
    agent = FakeAgent(["..."], block=True)
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            device_id, token, _ = await c.pair()
            await c.send("text", text="work")
            await c.until(is_type("state", screen="thinking"))
        # Dropped mid-turn: the device is unregistered, but the turn keeps running so its
        # reply can reach the next connection (owner, 2026-11-07: never lose the question).
        for _ in range(50):
            if device_id not in hub.sessions:
                break
            await asyncio.sleep(0.02)
        assert device_id not in hub.sessions
        async with Client.open(hub) as c:
            await c.hello(device_id, token=token)
            assert (await c.expect("welcome"))["state"] == "idle"
            assert hub.sessions[device_id].screen == "idle"


async def test_new_connection_replaces_old_one(state_dir):
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as old:
            device_id, token, _ = await old.pair()
            async with Client.open(hub) as new:
                await new.hello(device_id, token=token)
                await new.expect("welcome")
                with pytest.raises(ConnectionClosed) as info:
                    await old.recv()
                assert info.value.rcvd.code == 4001
                assert hub.sessions[device_id].ws is not None
                await new.send("ping", t=1)
                await new.expect("pong")
            await asyncio.sleep(0.05)
        assert device_id not in hub.sessions


async def test_silent_device_is_dropped(state_dir):
    async with running_hub(state_dir, idle_timeout=0.3) as hub, Client.open(hub) as c:
        device_id, _, _ = await c.pair()
        with pytest.raises(ConnectionClosed) as info:
            await c.recv(timeout=3)
        assert info.value.rcvd.code == 4002
        await asyncio.sleep(0.05)
        assert device_id not in hub.sessions


async def test_pings_keep_device_alive(state_dir):
    async with running_hub(state_dir, idle_timeout=0.4) as hub, Client.open(hub) as c:
        device_id, _, _ = await c.pair()
        for i in range(5):
            await asyncio.sleep(0.2)
            await c.send("ping", t=i)
            await c.expect("pong")
        assert device_id in hub.sessions


async def test_live_snapshot_tracks_connections(state_dir):
    async with running_hub(state_dir) as hub:
        live = json.loads((state_dir / "live.json").read_text())
        assert live["devices"] == [] and live["port"] == hub.port
        async with Client.open(hub) as c:
            device_id, _, _ = await c.pair(caps={"push": True, "haptic": False})
            live = json.loads((state_dir / "live.json").read_text())
            assert [d["device_id"] for d in live["devices"]] == [device_id]
            assert live["devices"][0]["caps"] == {"push": True, "haptic": False, "led": False, "battery": False}
        await asyncio.sleep(0.05)
        assert json.loads((state_dir / "live.json").read_text())["devices"] == []
    assert not (state_dir / "live.json").exists()


async def test_reply_finished_while_offline_is_spoken_on_reconnect(state_dir):
    release = asyncio.Event()

    class SlowAgent(FakeAgent):
        async def run(self, device, text):
            self.calls.append((device.device_id, text))
            self.started.set()
            await release.wait()
            yield "Here is the answer you missed."

    agent = SlowAgent()
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            device_id, token, _ = await c.pair()
            await c.send("text", text="a long question")
            await asyncio.wait_for(agent.started.wait(), 2)
        for _ in range(50):
            if device_id not in hub.sessions:
                break
            await asyncio.sleep(0.02)
        release.set()
        await asyncio.sleep(0.2)
        async with Client.open(hub) as c:
            await c.hello(device_id, token=token)
            await c.expect("welcome")
            msgs = await c.until(lambda m: isinstance(m, dict) and m.get("type") == "transcript"
                                 and m.get("final") and "you missed" in str(m.get("text")))
            assert any(m.get("role") == "assistant" for m in msgs if isinstance(m, dict))


async def test_reply_reaches_a_watch_that_came_back_mid_turn(state_dir):
    release = asyncio.Event()

    class SlowAgent(FakeAgent):
        async def run(self, device, text):
            self.calls.append((device.device_id, text))
            self.started.set()
            await release.wait()
            yield "Late but not lost."

    agent = SlowAgent()
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            device_id, token, _ = await c.pair()
            await c.send("text", text="question")
            await asyncio.wait_for(agent.started.wait(), 2)
        async with Client.open(hub) as c:
            await c.hello(device_id, token=token)
            await c.expect("welcome")
            release.set()
            msgs = await c.until(lambda m: isinstance(m, dict) and m.get("type") == "transcript"
                                 and m.get("final") and "not lost" in str(m.get("text")))
            assert msgs


async def test_question_waiting_in_line_is_asked_after_reconnect(state_dir):
    release = asyncio.Event()

    class SlowFirst(FakeAgent):
        async def run(self, device, text):
            self.calls.append((device.device_id, text))
            self.started.set()
            if text == "first":
                await release.wait()
            yield f"answer to {text}"

    agent = SlowFirst()
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            device_id, token, _ = await c.pair()
            await c.send("text", text="first")
            await asyncio.wait_for(agent.started.wait(), 2)
            await c.send("text", text="second")
            await asyncio.sleep(0.1)
        for _ in range(50):
            if device_id not in hub.sessions:
                break
            await asyncio.sleep(0.02)
        release.set()
        await asyncio.sleep(0.2)
        async with Client.open(hub) as c:
            await c.hello(device_id, token=token)
            await c.expect("welcome")
            await c.until(lambda m: isinstance(m, dict) and m.get("type") == "transcript"
                          and m.get("final") and "answer to second" in str(m.get("text")))
        assert [t for _, t in agent.calls] == ["first", "second"]
