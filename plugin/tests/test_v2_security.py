"""Protocol v2 security tests: owner approval, pending limits, revoke, error hygiene.

Behaviour is asserted through the public device socket, the loopback control
channel, and Store. Frames use v2 helpers because Client.send/expect are v1.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import AsyncExitStack

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from hail_hermes.hub import HubConfig
from hail_hermes.store import Store

from conftest import Client, FakeAgent, running_hub

_CAPS = {"push": True, "haptic": True, "led": False, "battery": True}
_LIMIT = 5.0


def _new_id() -> str:
    return str(uuid.uuid4())


async def send2(c, type_, **fields) -> None:
    await c.send_raw(json.dumps({"type": type_, "v": 2, **fields}))


async def expect2(c, type_, timeout: float = _LIMIT) -> dict:
    try:
        msg = await c.recv(timeout)
    except ConnectionClosed as exc:
        closed = exc.rcvd.code if exc.rcvd is not None else None
        assert False, f"expected {type_} v=2, socket closed {closed}"
    assert isinstance(msg, dict) and msg.get("type") == type_ and msg.get("v") == 2, msg
    return msg


async def silent(c, timeout: float = 0.2) -> None:
    try:
        msg = await c.recv(timeout)
    except asyncio.TimeoutError:
        return
    except ConnectionClosed as exc:
        closed = exc.rcvd.code if exc.rcvd is not None else None
        assert False, f"socket closed ({closed}) while expecting silence"
    assert False, f"unexpected message {msg!r}"


async def hello2(c, device_id: str, token: str | None = None, *, name: str = "test", kind: str = "sim") -> None:
    fields = {"device_id": device_id, "name": name, "kind": kind, "caps": dict(_CAPS)}
    if token is not None:
        fields["token"] = token
    await send2(c, "hello", **fields)


async def control(state_dir, hub, op: str, args: dict | None = None, *, req_id: str = "x") -> dict:
    token = Store(state_dir).control_token(create=False)
    assert isinstance(token, str) and token, "hub did not publish a control token"
    payload = {"id": req_id, "op": op, "args": {} if args is None else args}
    try:
        async with connect(
            hub.control_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=_LIMIT,
        ) as ws:
            await ws.send(json.dumps(payload))
            try:
                raw = await asyncio.wait_for(ws.recv(), _LIMIT)
            except asyncio.TimeoutError:
                assert False, f"timed out waiting for control reply to {op}"
    except ConnectionClosed as exc:
        closed = exc.rcvd.code if exc.rcvd is not None else None
        assert False, f"control socket closed ({closed}) during {op}"
    reply = json.loads(raw)
    assert isinstance(reply, dict) and reply.get("id") == req_id, reply
    return reply


def _assert_pending(msg: dict) -> dict:
    request_id = msg.get("request_id")
    match_code = msg.get("match_code")
    expires_in = msg.get("expires_in")
    assert isinstance(request_id, str) and len(request_id) >= 16
    assert isinstance(match_code, str) and re.fullmatch(r"[0-9]{4}", match_code)
    assert type(expires_in) in (int, float) and expires_in > 0
    return msg


def _assert_welcome(msg: dict, device_id: str) -> dict:
    assert msg.get("device_id") == device_id
    assert msg.get("state") == "idle"
    assert isinstance(msg.get("mascot_hash"), str)
    return msg


async def pair_via_owner(c, state_dir, hub, device_id: str, *, name: str = "test", kind: str = "sim") -> str:
    await hello2(c, device_id, name=name, kind=kind)
    pending = _assert_pending(await expect2(c, "pair_pending"))
    reply = await control(
        state_dir, hub, "pair_approve", {"match_code": pending["match_code"]}, req_id="approve",
    )
    assert reply.get("ok") is True, reply
    result = reply.get("result")
    assert isinstance(result, dict) and result.get("device_id") == device_id
    paired = await expect2(c, "paired")
    token = paired.get("token")
    assert isinstance(token, str) and len(token) >= 32
    _assert_welcome(await expect2(c, "welcome"), device_id)
    return token


async def assert_close(c, code: int, timeout: float = _LIMIT) -> None:
    """Next close frame must carry ``code``. A preceding error or pong is allowed."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out waiting for close {code}"
        try:
            msg = await c.recv(timeout=remaining)
        except asyncio.TimeoutError:
            assert False, f"timed out waiting for close {code}"
        except ConnectionClosed as exc:
            assert exc.rcvd is not None and exc.rcvd.code == code
            return
        if isinstance(msg, dict) and msg.get("type") == "pong":
            continue
        assert isinstance(msg, dict) and msg.get("type") == "error" and msg.get("v") == 2, msg


async def expect_error(c, code: str, timeout: float = _LIMIT) -> dict:
    """Session error, skipping a v2 state push that may precede it."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out waiting for error {code}"
        try:
            msg = await c.recv(timeout=min(_LIMIT, remaining))
        except asyncio.TimeoutError:
            assert False, f"timed out waiting for error {code}"
        except ConnectionClosed as exc:
            closed = exc.rcvd.code if exc.rcvd is not None else None
            assert False, f"closed ({closed}) waiting for error {code}"
        if isinstance(msg, dict) and msg.get("type") == "state" and msg.get("v") == 2:
            continue
        assert isinstance(msg, dict) and msg.get("type") == "error" and msg.get("v") == 2, msg
        assert msg.get("code") == code
        return msg


def _has_token(state_dir, device_id: str) -> bool:
    path = state_dir / "devices.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    devices = data.get("devices") if isinstance(data, dict) else None
    entry = devices.get(device_id) if isinstance(devices, dict) else None
    return isinstance(entry, dict) and "token_sha256" in entry


async def _reject_echo(hub, agent, version: int) -> None:
    """Hello, then echo every pairing secret back as pair_confirm v2 and v1."""
    device_id = _new_id()
    async with Client.open(hub) as c:
        await c.send_raw(json.dumps({
            "type": "hello",
            "v": version,
            "device_id": device_id,
            "name": "exploit",
            "kind": "sim",
            "caps": dict(_CAPS),
        }))
        msgs: list = []
        try:
            msgs.append(await c.recv(timeout=_LIMIT))
        except ConnectionClosed:
            pass
        except asyncio.TimeoutError:
            assert False, "timed out waiting for a reply to hello"
        else:
            while len(msgs) < 8:
                try:
                    msgs.append(await c.recv(timeout=0.05))
                except asyncio.TimeoutError:
                    break
                except ConnectionClosed:
                    break
            secrets: list[str] = []
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                for key in ("code", "request_id", "match_code"):
                    val = msg.get(key)
                    if isinstance(val, str) and val not in secrets:
                        secrets.append(val)
            initial = len(msgs)
            try:
                # v2 first, while a version-strict hub still has the socket open.
                for ver in (2, 1):
                    for secret in secrets:
                        await c.send_raw(json.dumps({
                            "type": "pair_confirm",
                            "v": ver,
                            "code": secret,
                            "request_id": secret,
                            "match_code": secret,
                        }))
                await send2(c, "text", text="pwned")
            except ConnectionClosed:
                pass
            deadline = time.monotonic() + 1.0
            while len(msgs) < 40 and time.monotonic() < deadline:
                try:
                    msgs.append(await c.recv(timeout=min(0.2, max(0.01, deadline - time.monotonic()))))
                except asyncio.TimeoutError:
                    if len(msgs) > initial:
                        break
                    continue
                except ConnectionClosed:
                    break
        types = [m.get("type") for m in msgs if isinstance(m, dict)]
        assert "paired" not in types
        assert "welcome" not in types
        assert agent.calls == []
        assert device_id not in hub.sessions


async def test_v2_echoing_pairing_secrets_does_not_authorize(state_dir):
    agent = FakeAgent()
    async with running_hub(state_dir, agent=agent) as hub:
        await _reject_echo(hub, agent, 2)
        await _reject_echo(hub, agent, 1)
        assert agent.calls == []


async def test_v2_unpaired_hello_gets_pair_pending(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await hello2(c, device_id, name="wrist", kind="wear")
        pending = _assert_pending(await expect2(c, "pair_pending"))
        assert pending["request_id"] != pending["match_code"]
        assert device_id not in hub.sessions


async def test_v2_owner_approval_then_token_reconnect(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            token = await pair_via_owner(c, state_dir, hub, device_id, name="watch", kind="wear")
            assert device_id in hub.sessions
        async with Client.open(hub) as c:
            await hello2(c, device_id, token=token, name="watch", kind="wear")
            _assert_welcome(await expect2(c, "welcome"), device_id)
            assert device_id in hub.sessions


async def test_v2_approval_is_bound_to_the_requesting_connection(state_dir):
    id_a, id_b = _new_id(), _new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as a, Client.open(hub) as b:
        await hello2(a, id_a, name="alpha", kind="wear")
        await hello2(b, id_b, name="beta", kind="sim")
        pend_a = _assert_pending(await expect2(a, "pair_pending"))
        pend_b = _assert_pending(await expect2(b, "pair_pending"))
        assert pend_a["request_id"] != pend_b["request_id"]
        reply = await control(
            state_dir, hub, "pair_approve", {"request_id": pend_a["request_id"]}, req_id="approve-a",
        )
        assert reply.get("ok") is True, reply
        assert (reply.get("result") or {}).get("device_id") == id_a
        await silent(b, 0.2)
        paired = await expect2(a, "paired")
        assert isinstance(paired.get("token"), str) and len(paired["token"]) >= 32
        _assert_welcome(await expect2(a, "welcome"), id_a)
        await silent(b, 0.2)
        listed = await control(state_dir, hub, "pair_list", req_id="list")
        assert listed.get("ok") is True, listed
        rows = listed.get("result")
        assert isinstance(rows, list)
        ids = [row.get("request_id") for row in rows]
        assert pend_b["request_id"] in ids
        assert pend_a["request_id"] not in ids
        still = next(row for row in rows if row.get("request_id") == pend_b["request_id"])
        assert still.get("match_code") == pend_b["match_code"]
        assert still.get("device_id") == id_b
        assert still.get("name") == "beta"
        assert still.get("kind") == "sim"
        assert id_a in hub.sessions
        assert id_b not in hub.sessions


async def test_v2_reapproval_and_unknown_request_are_rejected(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await hello2(c, device_id)
        pending = _assert_pending(await expect2(c, "pair_pending"))
        request_id = pending["request_id"]
        unknown = await control(
            state_dir, hub, "pair_approve",
            {"request_id": "missing-" + uuid.uuid4().hex}, req_id="unknown",
        )
        assert unknown.get("ok") is False
        once = await control(
            state_dir, hub, "pair_approve", {"request_id": request_id}, req_id="once",
        )
        assert once.get("ok") is True, once
        assert (once.get("result") or {}).get("device_id") == device_id
        token = (await expect2(c, "paired")).get("token")
        assert isinstance(token, str) and len(token) >= 32
        _assert_welcome(await expect2(c, "welcome"), device_id)
        twice = await control(
            state_dir, hub, "pair_approve", {"request_id": request_id}, req_id="twice",
        )
        assert twice.get("ok") is False
        await silent(c, 0.2)


async def test_v2_approval_after_disconnect_issues_nothing(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            await hello2(c, device_id, name="ghost", kind="sim")
            pending = _assert_pending(await expect2(c, "pair_pending"))
            request_id = pending["request_id"]
        reply = await control(
            state_dir, hub, "pair_approve", {"request_id": request_id}, req_id="late",
        )
        assert reply.get("ok") is False
        assert not _has_token(state_dir, device_id)
        async with Client.open(hub) as c:
            await hello2(c, device_id, name="ghost", kind="sim")
            again = _assert_pending(await expect2(c, "pair_pending"))
            assert again["request_id"] != request_id
            assert device_id not in hub.sessions


async def test_v2_deny_sends_denied_and_closes(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await hello2(c, device_id, name="nope", kind="sim")
        pending = _assert_pending(await expect2(c, "pair_pending"))
        reply = await control(
            state_dir, hub, "pair_deny", {"request_id": pending["request_id"]}, req_id="deny",
        )
        assert reply.get("ok") is True, reply
        err = await expect2(c, "error")
        assert err.get("code") == "denied"
        await assert_close(c, 4003)
        assert device_id not in hub.sessions
        assert not _has_token(state_dir, device_id)


async def test_v2_pending_deadline_ignores_pings(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir, pair_timeout=0.5) as hub, Client.open(hub) as c:
        started = time.monotonic()
        await hello2(c, device_id)
        _assert_pending(await expect2(c, "pair_pending"))

        async def pinger() -> None:
            try:
                while True:
                    await send2(c, "ping", t=1)
                    await asyncio.sleep(0.1)
            except (ConnectionClosed, asyncio.CancelledError):
                return

        ping_task = asyncio.create_task(pinger())
        err = None
        try:
            while time.monotonic() - started < 1.5:
                try:
                    msg = await c.recv(timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed as exc:
                    closed = exc.rcvd.code if exc.rcvd is not None else None
                    assert False, f"closed ({closed}) before pair_timeout"
                if isinstance(msg, dict) and msg.get("type") == "pong":
                    continue
                err = msg
                break
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except (asyncio.CancelledError, ConnectionClosed):
                pass
        elapsed = time.monotonic() - started
        assert elapsed <= 1.5
        assert isinstance(err, dict) and err.get("type") == "error" and err.get("v") == 2, err
        assert err.get("code") == "pair_timeout"
        await assert_close(c, 4003)
        assert device_id not in hub.sessions
        assert hub.agent.calls == []


async def test_v2_pending_rejects_text_audio_and_binary(state_dir):
    agent = FakeAgent()
    device_id = _new_id()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await hello2(c, device_id)
        _assert_pending(await expect2(c, "pair_pending"))
        await send2(c, "text", text="sneaky")
        assert (await expect2(c, "error")).get("code") == "not_paired"
        await send2(c, "audio_start", codec="pcm16le", rate=16000)
        assert (await expect2(c, "error")).get("code") == "not_paired"
        try:
            await c.send_raw(b"\x00\x01")
        except ConnectionClosed as exc:
            closed = exc.rcvd.code if exc.rcvd is not None else None
            assert False, f"connection closed ({closed}) instead of rejecting binary audio"
        assert (await expect2(c, "error")).get("code") == "not_paired"
        await asyncio.sleep(0.05)
        assert agent.calls == []
        assert device_id not in hub.sessions


async def test_v2_fourth_pending_client_from_same_ip_is_busy(state_dir):
    async with running_hub(state_dir) as hub:
        assert hub.url.startswith("ws://127.0.0.1:")
        async with AsyncExitStack() as stack:
            clients = []
            for _ in range(4):
                clients.append(await stack.enter_async_context(Client.open(hub)))
            ids = [_new_id() for _ in range(4)]
            for client, device_id in zip(clients[:3], ids[:3]):
                await hello2(client, device_id, name="waiter", kind="sim")
            for client, device_id in zip(clients[:3], ids[:3]):
                _assert_pending(await expect2(client, "pair_pending"))
                assert device_id not in hub.sessions
            await hello2(clients[3], ids[3], name="overflow", kind="sim")
            err = await expect2(clients[3], "error")
            assert err.get("code") == "busy"
            await assert_close(clients[3], 4003)
            for client in clients[:3]:
                await silent(client, 0.1)
            assert ids[3] not in hub.sessions


async def test_v2_version_is_checked_on_every_frame(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await pair_via_owner(c, state_dir, hub, device_id)
        await send2(c, "ping", t=3)
        pong = await expect2(c, "pong")
        assert pong.get("t") == 3
        await c.send_raw(json.dumps({"type": "ping", "v": 1, "t": 4}))
        err = await expect2(c, "error")
        assert err.get("code") == "version"
        await assert_close(c, 4000)


async def test_v2_odd_audio_frame_is_bad_frame(state_dir):
    agent = FakeAgent()
    device_id = _new_id()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await pair_via_owner(c, state_dir, hub, device_id)
        await send2(c, "audio_start", codec="pcm16le", rate=16000)
        try:
            await c.send_raw(b"\x00")
        except ConnectionClosed as exc:
            closed = exc.rcvd.code if exc.rcvd is not None else None
            assert False, f"connection closed ({closed}) before the odd frame was rejected"
        await expect_error(c, "bad_frame")
        await asyncio.sleep(0.05)
        assert agent.calls == []


async def test_v2_text_over_4000_is_too_long(state_dir):
    agent = FakeAgent()
    device_id = _new_id()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await pair_via_owner(c, state_dir, hub, device_id)
        await send2(c, "text", text="z" * 4001)
        await expect_error(c, "too_long")
        await asyncio.sleep(0.05)
        assert agent.calls == []


async def test_v2_revoke_closes_live_session_and_kills_token(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            token = await pair_via_owner(c, state_dir, hub, device_id, name="live", kind="wear")
            assert device_id in hub.sessions
            reply = await control(
                state_dir, hub, "revoke", {"device_id": device_id}, req_id="revoke",
            )
            assert reply.get("ok") is True, reply
            await assert_close(c, 4005)
        for _ in range(50):
            if device_id not in hub.sessions:
                break
            await asyncio.sleep(0.02)
        assert device_id not in hub.sessions
        async with Client.open(hub) as c:
            await hello2(c, device_id, token=token, name="live", kind="wear")
            _assert_pending(await expect2(c, "pair_pending"))
            await silent(c, 0.2)
            assert device_id not in hub.sessions


async def test_v2_revoke_from_a_separate_store_is_honoured_on_hello(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            token = await pair_via_owner(c, state_dir, hub, device_id, name="other", kind="sim")
        other = Store(state_dir)
        assert other is not hub.store
        assert other.revoke(device_id) is True
        async with Client.open(hub) as c:
            await hello2(c, device_id, token=token, name="other", kind="sim")
            _assert_pending(await expect2(c, "pair_pending"))
            await silent(c, 0.2)
            assert device_id not in hub.sessions


async def test_v2_agent_failure_hides_internals(state_dir):
    secret = "SECRET-stderr /home/x token=abc"
    agent = FakeAgent(fail=secret)
    device_id = _new_id()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await pair_via_owner(c, state_dir, hub, device_id)
        await send2(c, "text", text="hi")
        deadline = time.monotonic() + _LIMIT
        err = None
        while err is None:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "timed out waiting for agent_failed"
            try:
                msg = await c.recv(timeout=remaining)
            except asyncio.TimeoutError:
                assert False, "timed out waiting for agent_failed"
            except ConnectionClosed as exc:
                closed = exc.rcvd.code if exc.rcvd is not None else None
                assert False, f"closed ({closed}) before agent_failed"
            blob = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else json.dumps(msg)
            assert "SECRET" not in blob
            if isinstance(msg, dict) and msg.get("type") == "error":
                err = msg
        assert err.get("v") == 2
        assert err.get("code") == "agent_failed"
        assert "SECRET" not in str(err.get("message", ""))
        assert agent.calls == [(device_id, "hi")]


def test_v2_default_bind_is_loopback():
    assert HubConfig().host == "127.0.0.1"


async def test_v2_pair_list_shows_pending_request_until_approval(state_dir):
    device_id = _new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await hello2(c, device_id, name="pocket-watch", kind="wear")
        pending = _assert_pending(await expect2(c, "pair_pending"))
        listed = await control(state_dir, hub, "pair_list", req_id="before")
        assert listed.get("ok") is True, listed
        rows = listed.get("result")
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        assert row.get("request_id") == pending["request_id"]
        assert row.get("match_code") == pending["match_code"]
        assert row.get("device_id") == device_id
        assert row.get("name") == "pocket-watch"
        assert row.get("kind") == "wear"
        reply = await control(
            state_dir, hub, "pair_approve", {"match_code": pending["match_code"]}, req_id="approve",
        )
        assert reply.get("ok") is True, reply
        token = (await expect2(c, "paired")).get("token")
        assert isinstance(token, str) and len(token) >= 32
        _assert_welcome(await expect2(c, "welcome"), device_id)
        after = await control(state_dir, hub, "pair_list", req_id="after")
        assert after.get("ok") is True, after
        assert after.get("result") == []
