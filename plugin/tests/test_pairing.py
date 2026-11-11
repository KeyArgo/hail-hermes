"""Owner-approved pairing, tokens, version checks, and handshake errors."""

import asyncio
import json
import time
import uuid

import pytest
from websockets.exceptions import ConnectionClosed

from hail_hermes.control import ControlClient
from hail_hermes.store import Store

from conftest import Client, running_hub


async def test_unpaired_hello_gets_pending_and_owner_sees_code(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        device_id = str(uuid.uuid4())
        await c.hello(device_id)
        pending = await c.expect("pair_pending")
        assert len(pending["match_code"]) == 4 and pending["match_code"].isdigit()
        requests = await asyncio.to_thread(ControlClient(state_dir).call, "pair_list")
        assert len(requests) == 1
        assert requests[0]["match_code"] == pending["match_code"]
        assert requests[0]["request_id"] == pending["request_id"]
        assert requests[0]["device_id"] == device_id


async def test_pairing_then_welcome(state_dir):
    async with running_hub(state_dir, mascot_hash="abc123") as hub, Client.open(hub) as c:
        device_id, token, welcome = await c.pair()
        assert token and len(token) >= 32
        assert welcome == {"type": "welcome", "v": 2, "device_id": device_id, "state": "idle",
                           "mascot_hash": "abc123"}
        assert device_id in hub.sessions


async def test_echoing_code_cannot_approve_pairing(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        device_id = str(uuid.uuid4())
        await c.hello(device_id)
        pending = await c.expect("pair_pending")
        await c.send("pair_confirm", code=pending["match_code"])
        assert (await c.expect("error"))["code"] == "not_paired"
        assert await c.silent()
        assert device_id not in hub.store.paired()
        result = await asyncio.to_thread(
            ControlClient(state_dir).call, "pair_approve", match_code=pending["match_code"]
        )
        assert result["device_id"] == device_id
        await c.expect("paired")
        await c.expect("welcome")


async def test_owner_can_deny_pairing(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        device_id = str(uuid.uuid4())
        await c.hello(device_id)
        pending = await c.expect("pair_pending")
        await asyncio.to_thread(ControlClient(state_dir).call, "pair_deny", request_id=pending["request_id"])
        assert (await c.expect("error"))["code"] == "denied"
        with pytest.raises(ConnectionClosed) as info:
            await c.recv()
        assert info.value.rcvd.code == 4003
        assert device_id not in hub.store.paired()


async def test_session_messages_rejected_before_pairing(state_dir):
    agent_calls = []
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.hello(str(uuid.uuid4()))
        await c.expect("pair_pending")
        await c.send("text", text="sneaky")
        assert (await c.expect("error"))["code"] == "not_paired"
        await c.send("ping", t=7)
        assert (await c.expect("pong"))["t"] == 7
        assert hub.agent.calls == agent_calls


async def test_token_reconnect_skips_pairing(state_dir):
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id, token, _ = await c.pair()
        async with Client.open(hub) as c:
            await c.hello(device_id, token=token)
            welcome = await c.expect("welcome")
            assert welcome["device_id"] == device_id and welcome["state"] == "idle"


async def test_token_survives_hub_restart_and_is_stored_hashed(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        device_id, token, _ = await c.pair(name="watch")
    raw = (state_dir / "devices.json").read_text()
    assert token not in raw
    assert json.loads(raw)["devices"][device_id]["name"] == "watch"
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.hello(device_id, token=token)
        await c.expect("welcome")


async def test_bad_token_or_other_device_token_requires_pairing(state_dir):
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            _, token, _ = await c.pair()
        async with Client.open(hub) as c:
            await c.hello(str(uuid.uuid4()), token=token)
            await c.expect("pair_pending")


async def test_revoked_device_must_repair(state_dir):
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id, token, _ = await c.pair()
        assert hub.store.revoke(device_id)
        async with Client.open(hub) as c:
            await c.hello(device_id, token=token)
            await c.expect("pair_pending")


async def test_version_mismatch_refused_with_descriptive_error(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.send_raw(json.dumps({"type": "hello", "v": 1, "device_id": "d", "kind": "sim", "caps": {}}))
        err = await c.expect("error")
        assert err["code"] == "version" and "v2" in err["message"]
        with pytest.raises(ConnectionClosed) as info:
            await c.recv()
        assert info.value.rcvd.code == 4000


@pytest.mark.parametrize("bad", [{"device_id": ""}, {"kind": "toaster"}, {"device_id": 5}])
async def test_malformed_hello_refused(state_dir, bad):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.send("hello", **{"device_id": "dev", "kind": "sim", "caps": {}, **bad})
        await c.expect("error")
        with pytest.raises(ConnectionClosed) as info:
            await c.recv()
        assert info.value.rcvd.code == 4004


async def test_first_message_must_be_hello(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.send("text", text="hi")
        assert (await c.expect("error"))["code"] == "not_paired"
        await c.send_raw("{not json")
        assert (await c.expect("error"))["code"] == "internal"
        await c.hello(str(uuid.uuid4()))
        await c.expect("pair_pending")


async def test_hello_timeout_closes(state_dir):
    async with running_hub(state_dir, hello_timeout=0.2) as hub, Client.open(hub) as c:
        with pytest.raises(ConnectionClosed) as info:
            await c.recv(timeout=2)
        assert info.value.rcvd.code == 4002


async def test_unknown_path_rejected(state_dir):
    from websockets.exceptions import InvalidStatus

    async with running_hub(state_dir) as hub:
        with pytest.raises(InvalidStatus) as info:
            async with Client.open(hub, path="/nope"):
                pass
        assert info.value.response.status_code == 404


def test_store_tokens(tmp_path):
    store = Store(tmp_path)
    tok = store.issue_token("d1", "phone", "phone")
    assert store.check_token("d1", tok)
    assert not store.check_token("d1", tok + "x")
    assert not store.check_token("d2", tok)
    assert not store.check_token("d1", None)
    store.set_agent_session("d1", "sess-1")
    assert Store(tmp_path).agent_session("d1") == "sess-1"
    assert "token_sha256" not in Store(tmp_path).paired()["d1"]


async def test_approval_after_device_disconnect_does_not_pair(state_dir):
    """The owner must never be told a device paired when it has already gone."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id = str(uuid.uuid4())
            await c.hello(device_id)
            pending = await c.expect("pair_pending")
        await asyncio.sleep(0.15)
        with pytest.raises(RuntimeError) as info:
            await asyncio.to_thread(
                ControlClient(state_dir).call, "pair_approve", match_code=pending["match_code"]
            )
        message = str(info.value)
        assert "stopped waiting" in message, message
        assert "new one" in message, message
        assert "not found" not in message, message
        assert device_id not in hub.store.paired()


async def test_expired_code_tells_the_owner_to_ask_for_a_new_one(state_dir):
    """The owner's "1421" that sat too long must get a sentence, not "not found"."""
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        device_id = str(uuid.uuid4())
        await c.hello(device_id)
        pending = await c.expect("pair_pending")
        for entry in list(hub.pending.values()):
            entry.deadline = time.monotonic() - 1
        with pytest.raises(RuntimeError) as info:
            await asyncio.to_thread(
                ControlClient(state_dir).call, "pair_approve", match_code=pending["match_code"]
            )
        message = str(info.value)
        assert "expired" in message.lower(), message
        assert "new one" in message, message
        assert "not found" not in message, message
        assert not message[0].isdigit(), message
        assert device_id not in hub.store.paired()


async def test_already_approved_code_says_so_instead_of_not_found(state_dir):
    """Approving the same code twice must not hand out a second token, and must say why."""
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        device_id = str(uuid.uuid4())
        await c.hello(device_id)
        pending = await c.expect("pair_pending")
        first = await asyncio.to_thread(
            ControlClient(state_dir).call, "pair_approve", match_code=pending["match_code"]
        )
        assert first["device_id"] == device_id
        await c.expect("paired")
        await c.expect("welcome")
        with pytest.raises(RuntimeError) as info:
            await asyncio.to_thread(
                ControlClient(state_dir).call, "pair_approve", match_code=pending["match_code"]
            )
        message = str(info.value)
        assert "already approved" in message.lower(), message
        assert "not found" not in message, message
        assert device_id in hub.store.paired()


async def test_old_code_after_a_newer_attempt_names_the_code_on_the_watch(state_dir):
    """A retry replaces the code. Typing the old one must point at the new one."""
    async with running_hub(state_dir) as hub:
        device_id = str(uuid.uuid4())
        async with Client.open(hub) as first, Client.open(hub) as second:
            await first.hello(device_id)
            old = await first.expect("pair_pending")
            await second.hello(device_id)
            new = await second.expect("pair_pending")
            assert new["match_code"] != old["match_code"]
            # The first watch gave up; its request is gone, the second one is still waiting.
            await first.ws.close()
            for _ in range(100):
                if old["request_id"] not in hub.pending:
                    break
                await asyncio.sleep(0.02)
            assert old["request_id"] not in hub.pending, "the dead request was not dropped"
            # The hub answers a dead request with a LookupError carrying the sentence, which the
            # control client surfaces; either type is acceptable as long as it is refused and the
            # message names the code the watch is actually showing.
            with pytest.raises((RuntimeError, LookupError)) as info:
                await asyncio.to_thread(
                    ControlClient(state_dir).call, "pair_approve", match_code=old["match_code"]
                )
            message = str(info.value)
            assert "earlier try" in message, message
            assert new["match_code"] in message, message
            result = await asyncio.to_thread(
                ControlClient(state_dir).call, "pair_approve", match_code=new["match_code"]
            )
            assert result["device_id"] == device_id


async def test_code_from_a_restarted_hub_says_it_is_no_longer_waiting(state_dir):
    """The pending request died with the old hub process; the owner needs a new code."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id = str(uuid.uuid4())
            await c.hello(device_id)
            pending = await c.expect("pair_pending")
    async with running_hub(state_dir):
        with pytest.raises(RuntimeError) as info:
            await asyncio.to_thread(
                ControlClient(state_dir).call, "pair_approve", match_code=pending["match_code"]
            )
        message = str(info.value)
        assert "not waiting" in message, message
        assert "new one" in message, message
        assert "not found" not in message, message


async def test_unknown_request_id_gets_a_plain_sentence(state_dir):
    async with running_hub(state_dir):
        with pytest.raises(RuntimeError) as info:
            await asyncio.to_thread(
                ControlClient(state_dir).call, "pair_approve", request_id="not-a-real-request"
            )
        message = str(info.value)
        assert "not waiting" in message, message
        assert "new one" in message, message
        assert "not found" not in message, message


async def test_hub_404_is_not_the_string_the_owner_saw(state_dir):
    """The pane's "404: detail not found" came from Hermes's route layer, not this hub.

    Pairing never goes over the hub's HTTP port, and the hub's own 404 says so in words.
    """
    import urllib.error
    import urllib.request

    async with running_hub(state_dir) as hub:
        def fetch():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{hub.port}/approve", timeout=2)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        code, body = await asyncio.to_thread(fetch)
        assert code == 404
        assert body == "Hail Hermes hub: connect to /pocket\n"
        assert "detail not found" not in body


def test_pane_turns_a_missing_route_into_a_sentence():
    """The literal "404: detail not found" must never reach the owner."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "hail_hermes" / "desktop" / "plugin.js").read_text(
        encoding="utf-8"
    )
    assert "function pairingErrorText" in source
    assert "detail not found" in source
    assert "pairingUnreachable" in source
    # Both the response body and the thrown transport error go through the mapping.
    assert "pairingErrorText(res?.error, res?.hint || t('pairingUnreachable'))" in source
    assert "pairingErrorText(err, t('pairingUnreachable'))" in source
    # The old raw display is gone.
    assert "err?.message || t('approveFailed')" not in source
    assert "err?.message || t(kind === 'approve' ? 'approveFailed' : 'denyFailed')" not in source


def test_pairing_never_approves_itself(monkeypatch):
    """A watch must never be let in without a person approving it.

    A "pair automatically if it is on the local network" shortcut was tried and removed:
    on a shared Wi-Fi it would hand anyone the owner's agent. This test exists so the idea
    cannot come back quietly.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "hail_hermes" / "hub.py").read_text(encoding="utf-8")
    for forbidden in ("auto_approving", "auto_approve_pairing", "_pair_window", "pair_open"):
        assert forbidden not in source, f"silent pairing shortcut came back: {forbidden}"
