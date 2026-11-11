"""Adversarial tests for the v2 device and control channels.

A failing assertion identifies a possible security or resource-exhaustion issue.
All network receives and synchronization waits have explicit timeouts.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import AsyncExitStack

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from hail_hermes.store import Store

from conftest import Client, FakeAgent, RecordingSTT, running_hub


TIMEOUT = 5.0
CAPS = {"push": True, "haptic": True, "battery": True}


def new_id() -> str:
    return str(uuid.uuid4())


async def control(state_dir, hub, op, args=None):
    """Send one authenticated control request over a fresh connection."""
    token = Store(state_dir).control_token(create=False)
    assert token
    async with connect(
        hub.control_url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=TIMEOUT,
        close_timeout=TIMEOUT,
    ) as ws:
        await asyncio.wait_for(
            ws.send(json.dumps({"id": "attack", "op": op, "args": args or {}})),
            TIMEOUT,
        )
        reply = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
    assert reply["id"] == "attack"
    return reply


async def pending(c, device_id, **fields):
    await c.hello(device_id, **fields)
    return await c.expect("pair_pending", timeout=TIMEOUT)


async def approve(c, state_dir, hub, device_id):
    request = await pending(c, device_id)
    reply = await control(
        state_dir, hub, "pair_approve", {"request_id": request["request_id"]}
    )
    assert reply["ok"], reply
    token = (await c.expect("paired", timeout=TIMEOUT))["token"]
    await c.expect("welcome", timeout=TIMEOUT)
    return token


async def rejected_upgrade(url, *, headers=None, origin=None):
    with pytest.raises(InvalidStatus) as exc:
        async with connect(
            url,
            additional_headers=headers,
            origin=origin,
            open_timeout=TIMEOUT,
            close_timeout=TIMEOUT,
        ):
            pass
    return exc.value.response.status_code


async def wait_for(predicate, timeout=TIMEOUT):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_device_cannot_approve_its_own_pending_request(state_dir):
    agent = FakeAgent()
    device_id = new_id()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        request = await pending(c, device_id)
        await c.send(
            "pair_approve",
            request_id=request["request_id"],
            match_code=request["match_code"],
        )
        assert (await c.expect("error"))["code"] == "not_paired"
        await c.send("text", text="run without approval")
        assert (await c.expect("error"))["code"] == "not_paired"
        assert device_id not in hub.sessions
        assert not agent.calls


async def test_replayed_request_id_cannot_approve_a_new_connection(state_dir):
    device_id = new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as first:
            old = await pending(first, device_id)
        await wait_for(lambda: old["request_id"] not in hub.pending)

        async with Client.open(hub) as second:
            new = await pending(second, device_id)
            reply = await control(
                state_dir, hub, "pair_approve", {"request_id": old["request_id"]}
            )
            assert not reply["ok"]
            assert new["request_id"] != old["request_id"]
            await second.silent(timeout=0.2)
            assert device_id not in hub.sessions


async def test_mismatched_request_id_and_match_code_are_rejected(state_dir):
    """Supplying both selectors must not silently ignore a conflicting code."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as a, Client.open(hub) as b:
            pa = await pending(a, new_id())
            pb = await pending(b, new_id())
            reply = await control(
                state_dir,
                hub,
                "pair_approve",
                {"request_id": pa["request_id"], "match_code": pb["match_code"]},
            )
            assert not reply["ok"], reply
            await a.silent(timeout=0.2)
            await b.silent(timeout=0.2)


async def test_concurrent_approvals_are_single_use(state_dir):
    device_id = new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        request = await pending(c, device_id)
        args = {"request_id": request["request_id"]}
        replies = await asyncio.wait_for(
            asyncio.gather(
                control(state_dir, hub, "pair_approve", args),
                control(state_dir, hub, "pair_approve", args),
            ),
            TIMEOUT,
        )
        assert sum(reply["ok"] is True for reply in replies) == 1
        await c.expect("paired")
        await c.expect("welcome")


async def test_paired_identity_without_token_cannot_replace_session(state_dir):
    device_id = new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as legitimate:
            await approve(legitimate, state_dir, hub, device_id)
            async with Client.open(hub) as attacker:
                await pending(attacker, device_id)
                await legitimate.send("ping", t="still-owned")
                assert (await legitimate.expect("pong"))["t"] == "still-owned"
                await attacker.silent(timeout=0.2)


async def test_garbage_token_cannot_impersonate_paired_device(state_dir):
    device_id = new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            token = await approve(c, state_dir, hub, device_id)
        async with Client.open(hub) as attacker:
            await pending(attacker, device_id, token=token + "garbage")
            assert device_id not in hub.sessions
            await attacker.silent(timeout=0.2)


async def test_token_for_one_device_does_not_authenticate_another(state_dir):
    id_a, id_b = new_id(), new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as a:
            token_a = await approve(a, state_dir, hub, id_a)
        async with Client.open(hub) as attacker:
            await pending(attacker, id_b, token=token_a)
            assert id_b not in hub.sessions
            await attacker.silent(timeout=0.2)


async def test_binary_before_hello_does_not_authorize(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.send_raw(b"\x00\x00")
        assert (await c.expect("error"))["code"] == "not_paired"
        device_id = new_id()
        await pending(c, device_id)
        assert device_id not in hub.sessions


async def test_extra_hello_fields_do_not_grant_authority(state_dir):
    device_id = new_id()
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.send(
            "hello",
            device_id=device_id,
            name="attacker",
            kind="sim",
            caps=CAPS,
            approved=True,
            owner=True,
            role="admin",
            pair_confirm={"approved": True},
        )
        await c.expect("pair_pending")
        await c.silent(timeout=0.2)
        assert device_id not in hub.sessions


async def test_oversized_unicode_name_is_rejected(state_dir):
    """A near-frame-limit name should not become persistent pairing metadata."""
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.hello(new_id(), name="界" * 100_000)
        response = await c.recv(timeout=TIMEOUT)
        assert isinstance(response, dict) and response["type"] == "error", response
        assert not hub.pending


async def test_nested_kind_does_not_create_a_pairing(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.send(
            "hello",
            device_id=new_id(),
            name="nested",
            kind={"kind": "sim"},
            caps={"push": {"enabled": True}},
        )
        response = await c.recv(timeout=TIMEOUT)
        assert response["type"] == "error"
        assert not hub.pending


async def test_prehello_connections_are_bounded(state_dir):
    """Clients that never send hello should not bypass the pending-client limit."""
    async with running_hub(state_dir, hello_timeout=10) as hub:
        async with AsyncExitStack() as stack:
            sockets = []
            for _ in range(12):
                ws = await stack.enter_async_context(
                    connect(hub.url, open_timeout=TIMEOUT, close_timeout=TIMEOUT)
                )
                sockets.append(ws)
            # A cap of eight would match the maximum pending pairing requests.
            # Ping over each socket to distinguish live connections from closed ones.
            live = 0
            for ws in sockets:
                try:
                    await asyncio.wait_for(
                        ws.send(json.dumps({"v": 2, "type": "ping", "t": 1})),
                        TIMEOUT,
                    )
                    reply = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
                    live += reply.get("type") == "pong"
                except ConnectionClosed:
                    pass
            assert live <= 8, f"{live} pre-hello sockets remained usable"


async def test_pending_ping_flood_does_not_extend_deadline(state_dir):
    async with running_hub(state_dir, pair_timeout=0.4) as hub, Client.open(hub) as c:
        device_id = new_id()
        await pending(c, device_id)

        async def flood():
            try:
                while True:
                    await c.send("ping", t=1)
                    await asyncio.sleep(0.01)
            except (ConnectionClosed, asyncio.CancelledError):
                pass

        task = asyncio.create_task(flood())
        try:
            async with asyncio.timeout(2):
                while True:
                    message = await c.recv(timeout=1)
                    if message.get("type") == "error":
                        assert message["code"] == "pair_timeout"
                        break
        finally:
            task.cancel()
            await asyncio.wait_for(task, TIMEOUT)
        assert device_id not in hub.sessions


async def test_incomplete_audio_is_discarded_on_disconnect(state_dir):
    agent = FakeAgent()
    stt = RecordingSTT("unexpected")
    device_id = new_id()
    async with running_hub(state_dir, agent=agent, stt=stt) as hub:
        async with Client.open(hub) as c:
            await approve(c, state_dir, hub, device_id)
            await c.send("audio_start")
            await c.send_raw(b"\x00\x00" * 6000)
            await c.expect("state")
        await wait_for(lambda: device_id not in hub.sessions)
        assert not stt.received
        assert not agent.calls


async def test_oversized_audio_is_discarded_without_stt(state_dir):
    agent = FakeAgent()
    stt = RecordingSTT("unexpected")
    async with running_hub(
        state_dir, agent=agent, stt=stt, max_audio_seconds=0.5
    ) as hub, Client.open(hub) as c:
        await approve(c, state_dir, hub, new_id())
        await c.send("audio_start")
        await c.expect("state")
        # Multiple legal WebSocket frames exceed the aggregate audio limit.
        await c.send_raw(b"\x00\x00" * 5000)
        await c.send_raw(b"\x00\x00" * 5000)
        assert (await c.expect("error"))["code"] == "too_long"
        await c.expect("state")
        await c.send("audio_end")
        await c.send("ping", t="barrier")
        assert (await c.expect("pong"))["t"] == "barrier"
        assert not stt.received
        assert not agent.calls


@pytest.mark.parametrize("auth", [None, "Bearer wrong-token"])
async def test_control_rejects_missing_or_wrong_authorization(state_dir, auth):
    async with running_hub(state_dir) as hub:
        headers = {"Authorization": auth} if auth else None
        assert await rejected_upgrade(hub.control_url, headers=headers) == 401


async def test_control_rejects_token_in_query_string(state_dir):
    async with running_hub(state_dir) as hub:
        token = Store(state_dir).control_token(create=False)
        assert await rejected_upgrade(f"{hub.control_url}?token={token}") == 401


async def test_control_path_traversal_does_not_reach_control(state_dir):
    async with running_hub(state_dir) as hub:
        token = Store(state_dir).control_token(create=False)
        url = f"ws://127.0.0.1:{hub.port}/pocket/../control"
        status = await rejected_upgrade(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        assert status == 404


async def test_foreign_browser_origin_cannot_open_device_socket(state_dir):
    """A hostile website must not be able to drive the local device endpoint."""
    async with running_hub(state_dir) as hub:
        status = await rejected_upgrade(hub.url, origin="https://attacker.example")
        assert status == 403


async def test_revoke_invalidates_a_blocked_turn(state_dir):
    agent = FakeAgent(chunks=(), block=True)
    device_id = new_id()
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        token = await approve(c, state_dir, hub, device_id)
        await c.send("text", text="sensitive turn")
        await asyncio.wait_for(agent.started.wait(), TIMEOUT)
        reply = await control(state_dir, hub, "revoke", {"device_id": device_id})
        assert reply["ok"] and reply["result"]["revoked"]
        await asyncio.wait_for(agent.cancelled.wait(), TIMEOUT)
        await wait_for(lambda: device_id not in hub.sessions)
        async with Client.open(hub) as retry:
            await pending(retry, device_id, token=token)
            assert device_id not in hub.sessions


async def test_agent_failure_does_not_disclose_secret(state_dir):
    secret = "private-agent-secret-52d821"
    agent = FakeAgent(fail=secret)
    async with running_hub(state_dir, agent=agent) as hub, Client.open(hub) as c:
        await approve(c, state_dir, hub, new_id())
        await c.send("text", text="trigger failure")
        async with asyncio.timeout(TIMEOUT):
            while True:
                message = await c.recv(timeout=TIMEOUT)
                assert secret not in str(message)
                if isinstance(message, dict) and message.get("type") == "error":
                    assert message["code"] == "agent_failed"
                    break


@pytest.mark.parametrize("op", ["say", "notify"])
async def test_control_cannot_target_disconnected_device(state_dir, op):
    device_id = new_id()
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            await approve(c, state_dir, hub, device_id)
        await wait_for(lambda: device_id not in hub.sessions)
        reply = await control(
            state_dir,
            hub,
            op,
            {"device_id": device_id, "text": "hello", "title": "hello", "body": "hello"},
        )
        assert not reply["ok"], reply
