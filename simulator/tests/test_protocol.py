import asyncio
import json
import socket

import websockets
from pocket_sim.hub import Hub
from pocket_sim.protocol import V

PORT = 8799
URL = f"ws://127.0.0.1:{PORT}/pocket"
CONTROL_URL = f"ws://127.0.0.1:{PORT}/control"


def _free_port() -> int:
    """An ephemeral port, so two runs — or a leftover server — cannot collide on a fixed one.

    These tests used to hard-code 8799. Any second process (a parallel agent's test run, a hub left
    up from a demo) made every test in the file fail with `OSError: [Errno 98] address already in
    use`, which reads as six broken tests rather than one busy port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def hello_payload(device_id, **fields):
    base = {
        "type": "hello",
        "v": V,
        "device_id": device_id,
        "name": "sim",
        "kind": "sim",
        "caps": {},
    }
    base.update(fields)
    return json.dumps(base)


async def recv_until(ws, typ, timeout=5):
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, (bytes, bytearray)):
            continue
        m = json.loads(raw)
        assert m.get("v") == V, m
        if m["type"] == typ:
            return m


async def control_approve(hub: Hub, pending: dict) -> None:
    headers = {"Authorization": f"Bearer {hub.control_token}"}
    async with websockets.connect(CONTROL_URL, additional_headers=headers) as cws:
        await cws.send(
            json.dumps(
                {
                    "id": "approve",
                    "op": "pair_approve",
                    "args": {"request_id": pending["request_id"]},
                }
            )
        )
        reply = json.loads(await cws.recv())
        assert reply.get("ok"), reply


async def pair_session(ws, hub: Hub, device_id: str, **hello_kw):
    await ws.send(hello_payload(device_id, **hello_kw))
    pending = await recv_until(ws, "pair_pending")
    assert pending["match_code"].isdigit() and len(pending["match_code"]) == 4
    assert pending["request_id"]
    assert pending["expires_in"] > 0
    if not hub.auto_approve:
        await control_approve(hub, pending)
    paired = await recv_until(ws, "paired")
    welcome = await recv_until(ws, "welcome")
    return pending, paired, welcome


async def with_hub(coro, **hub_kw):
    global PORT, URL, CONTROL_URL
    PORT = _free_port()
    URL = f"ws://127.0.0.1:{PORT}/pocket"
    CONTROL_URL = f"ws://127.0.0.1:{PORT}/control"
    h = Hub(
        host="127.0.0.1",
        port=PORT,
        echo_replies=["hi there friend"],
        stt_text="heard",
        tts=True,
        **hub_kw,
    )
    server = await websockets.serve(h.handler, h.host, h.port, reuse_address=True)
    try:
        await coro(h)
    finally:
        server.close()
        await server.wait_closed()


def run(coro, **hub_kw):
    asyncio.run(with_hub(coro, **hub_kw))


def test_pairing_and_welcome():
    async def body(h):
        async with websockets.connect(URL) as ws:
            pending, paired, welcome = await pair_session(ws, h, "d1", caps={"push": True})
            assert "token" in paired
            assert welcome["device_id"] == "d1"
            assert welcome["state"] == "idle"
            assert len(pending["match_code"]) == 4

    run(body, auto_approve=False)


def test_pairing_auto_approve():
    async def body(h):
        async with websockets.connect(URL) as ws:
            _, paired, welcome = await pair_session(ws, h, "d-auto")
            assert paired["token"]
            assert welcome["device_id"] == "d-auto"

    run(body, auto_approve=True)


def test_ping_pong():
    async def body(h):
        async with websockets.connect(URL) as ws:
            await pair_session(ws, h, "d5")
            await ws.send(json.dumps({"type": "ping", "v": V, "t": 42}))
            p = await recv_until(ws, "pong")
            assert p["t"] == 42

    run(body, auto_approve=True)


def test_reconnect():
    async def body(h):
        async with websockets.connect(URL) as ws:
            _, paired, _ = await pair_session(ws, h, "d6")
            token = paired["token"]
        async with websockets.connect(URL) as ws:
            await ws.send(hello_payload("d6", token=token))
            w = await recv_until(ws, "welcome")
            assert w["device_id"] == "d6"
            assert w["state"] == "idle"

    run(body, auto_approve=True)


def test_text_roundtrip():
    async def body(h):
        async with websockets.connect(URL) as ws:
            await pair_session(ws, h, "d3")
            await ws.send(json.dumps({"type": "text", "v": V, "text": "hi"}))
            tu = await recv_until(ws, "transcript")
            assert tu["role"] == "user"
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                m = json.loads(raw)
                if m["type"] == "transcript" and m["role"] == "assistant" and m.get("final"):
                    assert m["text"]
                    break

    run(body, auto_approve=True)


def test_bad_protocol_version():
    async def body(h):
        async with websockets.connect(URL) as ws:
            await ws.send(json.dumps({"type": "hello", "v": 1, "device_id": "d-bad", "kind": "sim"}))
            err = await recv_until(ws, "error")
            assert err["code"] == "version"
            assert err["message"]

    run(body, auto_approve=True)


async def with_hub_ret(coro, **hub_kw):
    global PORT, URL, CONTROL_URL
    PORT = _free_port()
    URL = f"ws://127.0.0.1:{PORT}/pocket"
    CONTROL_URL = f"ws://127.0.0.1:{PORT}/control"
    h = Hub(
        host="127.0.0.1",
        port=PORT,
        echo_replies=["hi there friend"],
        stt_text="heard",
        tts=True,
        **hub_kw,
    )
    server = await websockets.serve(h.handler, h.host, h.port, reuse_address=True)
    try:
        return await coro(h)
    finally:
        server.close()
        await server.wait_closed()
