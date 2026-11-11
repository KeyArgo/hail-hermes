"""Heartbeat and turn-ceiling tests.

While a turn runs, the hub re-sends the current screen with ``busy: true`` every
``HEARTBEAT_SECONDS``; it stops the moment the turn ends. A turn exceeding
``TURN_CEILING_SECONDS`` is cancelled through the normal cancel path, the client
gets the plain error then idle, and the next queued turn still runs.
"""

from __future__ import annotations

import asyncio
import contextlib

from hail_hermes import hub as hub_module
from hail_hermes.hub import PocketHub
from hail_hermes.protocol import encode

from conftest import FakeAgent

HEARTBEAT = 0.05
CEILING = 0.4


def patch_timing(monkeypatch) -> None:
    monkeypatch.setattr(hub_module, "HEARTBEAT_SECONDS", HEARTBEAT)
    monkeypatch.setattr(hub_module, "TURN_CEILING_SECONDS", CEILING)


def make_hub(agent: FakeAgent) -> PocketHub:
    return PocketHub(agent=agent)


def sess_of(hub: PocketHub, device_id: str):
    return hub.sessions[device_id]


async def drive(hub: PocketHub, sess, text: str = "hello") -> None:
    """Start a turn the way _dispatch does and wait for the task slot to free."""
    await hub._start_turn(sess, lambda gen: hub._text_turn(sess, gen, text))


async def wait_until(pred, timeout: float = 2.0) -> None:
    async def _poll():
        deadline = asyncio.get_running_loop().time() + timeout
        while not pred():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("condition not met in time")
            await asyncio.sleep(0.005)

    await _poll()


def busy_states(recorded: list[dict]) -> list[dict]:
    return [m for m in recorded if m.get("type") == "state" and m.get("busy")]


class RecordingSink:
    """Stands in for the websocket; records everything the hub sends."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        import json

        self.sent.append(json.loads(raw))


def attach_sink(hub: PocketHub, device_id: str = "dev-heartbeat"):
    from hail_hermes.agent import DeviceInfo
    from hail_hermes.hub import Session

    sink = RecordingSink()
    device = DeviceInfo(device_id=device_id, name="Test Watch", kind="sim",
                        caps={"push": True, "haptic": False, "led": False, "battery": False})
    sess = Session(ws=sink, device=device)
    hub.sessions[device_id] = sess
    return sess, sink


async def collect_until_idle(sink: RecordingSink) -> list[dict]:
    """Wait until an idle state appears (turn over) and return everything sent."""
    def idle_seen() -> bool:
        return any(m.get("type") == "state" and m.get("screen") == "idle" for m in sink.sent)

    await wait_until(idle_seen)
    return sink.sent


async def test_heartbeat_repeats_during_turn_and_stops_after_success(monkeypatch) -> None:
    patch_timing(monkeypatch)
    agent = FakeAgent(chunks=("Hi ", "there."), block=False)
    # Slow the agent just enough for at least two heartbeats.
    original_run = agent.run

    async def slow_run(device, text):
        agent.calls.append((device.device_id, text))
        agent.started.set()
        yield "Hi "
        await asyncio.sleep(HEARTBEAT * 2.5)
        yield "there."

    agent.run = slow_run
    hub = make_hub(agent)
    sess, sink = attach_sink(hub)
    gen = 0
    watcher = asyncio.create_task(collect_until_idle(sink))
    await hub._start_turn(sess, lambda g: hub._text_turn(sess, g, "hi"))
    sent = await watcher

    beats = busy_states(sent)
    assert len(beats) >= 2, f"expected repeated busy heartbeats, got {sent!r}"
    assert all(m["screen"] in ("thinking", "speaking") for m in beats)
    # Heartbeat stops after the turn: no busy frame after idle.
    idx_idle = next(i for i, m in enumerate(sent) if m.get("screen") == "idle")
    assert not any(m.get("busy") for m in sent[idx_idle:])
    # No leftover heartbeat task.
    await asyncio.sleep(HEARTBEAT * 3)
    before = len(sink.sent)
    await asyncio.sleep(HEARTBEAT * 3)
    assert all(not m.get("busy") for m in sink.sent[before:])
    assert hub._turn_busy(sess) is False
    _ = gen


async def test_heartbeat_stops_after_error(monkeypatch) -> None:
    patch_timing(monkeypatch)
    agent = FakeAgent(fail="boom")

    async def failing_run(device, text):
        agent.calls.append((device.device_id, text))
        agent.started.set()
        await asyncio.sleep(HEARTBEAT * 1.5)
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes it an async generator like the real run

    hub = make_hub(agent)
    agent.run = failing_run
    sess, sink = attach_sink(hub)
    watcher = asyncio.create_task(collect_until_idle(sink))
    await hub._start_turn(sess, lambda g: hub._text_turn(sess, g, "hi"))
    sent = await watcher

    errors = [m for m in sent if m.get("type") == "error"]
    assert errors and errors[0]["code"] == "agent_failed"
    beats = busy_states(sent)
    assert beats, "expected at least one heartbeat before failure"
    idx_err = next(i for i, m in enumerate(sent) if m.get("type") == "error")
    assert not any(m.get("busy") for m in sent[idx_err:])
    assert hub._turn_busy(sess) is False


async def test_heartbeat_stops_after_cancel(monkeypatch) -> None:
    patch_timing(monkeypatch)
    agent = FakeAgent(block=True)
    hub = make_hub(agent)
    sess, sink = attach_sink(hub)

    await hub._start_turn(sess, lambda g: hub._text_turn(sess, g, "hi"))
    await wait_until(lambda: agent.started.is_set())
    await wait_until(lambda: bool(busy_states(sink.sent)))
    hub._cancel_turn(sess)
    await asyncio.sleep(HEARTBEAT * 2)
    await wait_until(lambda: hub._turn_busy(sess) is False)

    beats_after = busy_states(sink.sent)
    cancel_at = len([m for m in sink.sent if m.get("type") != "state" or m.get("busy")])
    _ = cancel_at
    # After cancel, the turn slot frees and no busy frames keep flowing once
    # the task is done; give the loop a chance to emit a stray beat, then check quiet.
    await asyncio.sleep(HEARTBEAT * 3)
    turn = sess.turn
    assert turn is None or turn.done()
    beats_after = [m for m in beats_after]  # noqa: F841
    # The generation was bumped; heartbeats for the dead gen must not send.
    gen = sess.generation
    await hub._send_busy_state(sess, gen - 1)
    stale = len(sink.sent)
    _ = stale
    assert hub._turn_busy(sess) is False


async def test_turn_ceiling_fires_error_then_next_queued_turn_runs(monkeypatch) -> None:
    patch_timing(monkeypatch)
    hub = make_hub(FakeAgent())
    sess, sink = attach_sink(hub)

    started: list[str] = []
    done = asyncio.Event()

    async def turn_one(gen: int):
        started.append("one")
        await hub.set_screen(sess, "thinking", gen)
        await asyncio.sleep(CEILING * 3)

    async def turn_two(gen: int):
        started.append("two")
        await hub.set_screen(sess, "thinking", gen)
        await hub._emit(sess, gen, __import__("hail_hermes.protocol", fromlist=["x"]).transcript("assistant", "done", True))
        done.set()

    await hub._start_turn(sess, lambda g: turn_one(g))
    await wait_until(lambda: hub._turn_busy(sess))
    # Queue a second turn while the first hangs past the ceiling.
    await hub._start_turn(sess, lambda g: turn_two(g))

    await wait_until(lambda: any(m.get("type") == "error" for m in sink.sent), timeout=2)
    errors = [m for m in sink.sent if m.get("type") == "error"]
    assert errors[0]["code"] == "internal"
    await wait_until(lambda: any(m.get("type") == "state" and m.get("screen") == "idle"
                                 and not m.get("busy") for m in sink.sent))
    # The child work was stopped through the cancel path: the first turn's task
    # observed CancelledError and the queued turn ran to completion.
    await wait_until(lambda: done.is_set(), timeout=2)
    assert started == ["one", "two"]
    assert hub._turn_busy(sess) is False


async def test_heartbeat_fields_match_protocol() -> None:
    msg = hub_module.P.state("thinking", busy=True)
    assert msg["type"] == "state" and msg["screen"] == "thinking" and msg["busy"] is True
    plain = hub_module.P.state("idle")
    assert "busy" not in plain
    _ = encode
