"""Device tools end to end: Hermes-style tool handler → control channel → hub → device."""

import asyncio
import json

import pytest

from hail_hermes.control import ControlClient, HubUnavailable
from hail_hermes.hermes_plugin import register
from hail_hermes.tools import TOOLS, make_check, make_handler

from conftest import Client, RecordingSTT, EchoTTS, is_type, running_hub


def call_tool(state_dir, op, **args) -> dict:
    return json.loads(make_handler(op, ControlClient(state_dir))(args, task_id="t1"))


async def tool(state_dir, op, **args) -> dict:
    # Tool handlers are synchronous (Hermes runs them in worker threads).
    return await asyncio.to_thread(call_tool, state_dir, op, **args)


async def test_notify_reaches_device(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        device_id, _, _ = await c.pair(kind="phone")
        res = await tool(state_dir, "notify", title="Sam", body="Reply by Friday")
        assert res == {"ok": True, "device_id": device_id, "delivered": True}
        assert await c.expect("notify") == {"type": "notify", "v": 2, "title": "Sam", "body": "Reply by Friday"}


async def test_haptic_sends_haptic_message(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair(kind="wear")
        res = await tool(state_dir, "haptic", pattern="double")
        assert res["ok"]
        assert await c.expect("haptic") == {"type": "haptic", "v": 2, "pattern": "double"}


async def test_say_speaks_on_device(state_dir):
    stt = RecordingSTT()
    async with running_hub(state_dir, tts=EchoTTS(stt)) as hub, Client.open(hub) as c:
        await c.pair()
        res = await tool(state_dir, "say", text="It's Thursday.")
        assert res["ok"] and res["audio"]
        msgs = await c.until(is_type("state", screen="idle"))
        # A spoken delivery says it is working before it publishes, so the closing idle is a real
        # transition rather than a duplicate the client never receives.
        assert msgs[0] == {"type": "state", "v": 2, "screen": "thinking"}
        spoken = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript"]
        assert spoken == [{"type": "transcript", "v": 2, "role": "assistant",
                           "text": "It's Thursday.", "final": True}]
        assert any(is_type("audio_start")(m) for m in msgs) and any(is_type("audio_end")(m) for m in msgs)


async def test_say_waits_for_running_turn(state_dir):
    from conftest import FakeAgent

    agent = FakeAgent(["working"], block=True)
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="go")
        await c.until(is_type("transcript", role="assistant"))
        res = await tool(state_dir, "say", text="later")
        assert res["queued"]
        assert await c.silent()  # not spoken over the running turn
        await c.send("interrupt")  # interrupt drops the queued utterance too
        await c.until(is_type("state", screen="idle"))
        assert await c.silent()


async def test_battery_from_hello_caps_and_report(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair(caps={"battery": {"mv": 3900, "charging": False}})
        assert await tool(state_dir, "battery") == {"ok": True, "device_id": c and next(iter(hub.sessions)),
                                                    "mv": 3900, "charging": False}
        await c.send("battery", mv=3700, charging=True)  # proposed extension message
        await c.send("ping", t=0)
        await c.expect("pong")
        res = await tool(state_dir, "battery")
        assert (res["mv"], res["charging"]) == (3700, True)


async def test_tools_respect_caps_and_targeting(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as a, Client.open(hub) as b:
        id_a, _, _ = await a.pair(caps={"push": True})
        id_b, _, _ = await b.pair(caps={"push": True, "haptic": True})
        res = await tool(state_dir, "haptic")  # only b has haptic → auto-targeted
        assert res["ok"] and res["device_id"] == id_b
        await b.expect("haptic")
        res = await tool(state_dir, "haptic", device_id=id_a)
        assert not res["ok"]
        assert "does not support" in res["error"]
        res = await tool(state_dir, "notify", title="t", body="b")  # ambiguous
        assert not res["ok"]
        assert "several devices" in res["error"]
        res = await tool(state_dir, "notify", title="t", body="b", device_id=id_a)
        assert res["ok"]
        await a.expect("notify")
        res = await tool(state_dir, "notify", title="t", body="b", device_id="ghost")
        assert not res["ok"]
        assert "not connected" in res["error"]


async def test_check_fn_follows_connected_caps(state_dir):
    client = ControlClient(state_dir)
    checks = {name: make_check(cap, client) for name, (cap, _, _) in TOOLS.items()}
    async with running_hub(state_dir) as hub:
        assert not any(fn() for fn in checks.values())
        async with Client.open(hub) as c:
            await c.pair(caps={"push": True, "haptic": False, "battery": False})
            assert checks["device_notify"]() and checks["device_say"]()
            assert not checks["device_haptic"]() and not checks["device_battery"]()
        await asyncio.sleep(0.05)
        assert not checks["device_notify"]()


def test_tools_without_hub_fail_softly(tmp_path):
    res = json.loads(make_handler("notify", ControlClient(tmp_path))({"title": "x", "body": "y"}))
    assert not res["ok"] and "not running" in res["error"]
    assert make_check("push", ControlClient(tmp_path))() is False
    with pytest.raises(HubUnavailable):
        ControlClient(tmp_path).call("devices")


async def test_control_channel_requires_token(state_dir):
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async with running_hub(state_dir) as hub:
        with pytest.raises(InvalidStatus) as info:
            async with connect(hub.control_url, additional_headers={"Authorization": "Bearer nope"}):
                pass
        assert info.value.response.status_code == 401
        with pytest.raises(InvalidStatus):
            async with connect(hub.control_url):
                pass
        assert await asyncio.to_thread(ControlClient(state_dir).call, "devices") == []


async def test_control_channel_rejects_non_loopback(state_dir, monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    from hail_hermes import hub as hub_mod
    from hail_hermes.store import Store

    monkeypatch.setattr(hub_mod, "_is_loopback", lambda _addr: False)
    token = Store(state_dir).control_token(create=False)
    async with running_hub(state_dir) as hub:
        with pytest.raises(InvalidStatus) as info:
            async with connect(
                hub.control_url,
                additional_headers={"Authorization": f"Bearer {token}"},
            ):
                pass
        assert info.value.response.status_code == 403


class FakeCtx:
    def __init__(self):
        self.tools, self.cli = {}, {}

    def get_config(self, key, default=None):
        return default

    def register_tool(self, name, toolset, schema, handler, check_fn=None, **kw):
        self.tools[name] = dict(toolset=toolset, schema=schema, handler=handler, check_fn=check_fn, **kw)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)


def test_register_wires_protocol_tools_and_cli():
    import hail_hermes

    ctx = FakeCtx()
    hail_hermes.register(ctx)
    assert set(ctx.tools) == {"device_notify", "device_say", "device_haptic", "device_battery"}
    for name, entry in ctx.tools.items():
        assert entry["toolset"] == "pocket"
        assert entry["schema"]["name"] == name
        assert entry["schema"]["parameters"]["type"] == "object"
        assert callable(entry["handler"]) and callable(entry["check_fn"])
    assert set(ctx.cli) == {"hail", "pocket"}

    import argparse

    parser = argparse.ArgumentParser()
    ctx.cli["hail"][0](parser)
    args = parser.parse_args(["serve", "--port", "9999", "--agent", "echo"])
    assert args.pocket_cmd == "serve" and args.port == 9999


def test_plugin_manifest_matches_registered_tools():
    from pathlib import Path

    import hail_hermes

    manifest = (Path(hail_hermes.__file__).parent / "plugin.yaml").read_text()
    for name in TOOLS:
        assert f"- {name}" in manifest
