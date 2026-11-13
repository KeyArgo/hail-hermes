"""A session must always come to rest on a screen that means "nothing is happening".

The bug this file pins was reported to the owner as a spinner that never stopped: interrupt a reply
at the moment a background hand-off result lands, and the last frame the watch receives can be
``thinking`` with no task left owning the turn slot to overwrite it. Only the task that owns the slot
emitted ``idle`` from its own ``finally``, so any interleaving that left two writers — a cancelled
turn, a queued turn, a late hand-off — could finish with nobody saying the answer was over.

Each test below drives one of those interleavings deliberately rather than hoping the scheduler
produces it, because the failure is a race: a test that waits for luck is the reason the bug reached
a release in the first place.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import Client, FakeAgent, is_type, running_hub


async def _wait_for(c, pred, timeout: float = 5.0):
    return await c.until(pred, timeout)


async def test_interrupt_mid_reply_leaves_the_watch_on_idle(state_dir):
    """The ordinary case, kept as the baseline: an interrupted turn ends on ``idle``."""
    agent = FakeAgent(chunks=("Partial ", "answer."), block=True)
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("text", text="Tell me a long story")
            await _wait_for(c, is_type("state", screen="thinking"))
            await agent.started.wait()
            await c.send("interrupt")
            msgs = await _wait_for(c, is_type("state", screen="idle"))
            assert msgs[-1]["screen"] == "idle"


async def test_a_turn_that_lost_the_slot_still_settles_the_screen(state_dir):
    """A cancelled task's ``finally`` used to emit nothing once it no longer owned the slot.

    This reproduces that shape without a scheduler race: the session's slot is emptied while the
    turn task is already finished, which is exactly what an interrupt plus a replacement turn leaves
    behind.
    """
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id, _, _ = await c.pair()
            sess = hub.sessions[device_id]

            # A task that has already run to completion, then lost the slot: the guard's own branch
            # (`sess.turn is asyncio.current_task()`) is false for it, so only `_settle` can speak.
            async def finished():
                return None

            done = asyncio.create_task(finished())
            await done                      # a Task that has already completed, like a cancelled one
            sess.turn = done
            sess.screen = "thinking"

            await hub._settle(sess)

            msg = await c.recv()
            assert msg["type"] == "state" and msg["screen"] == "idle", msg


async def test_settle_does_not_cut_in_front_of_a_queued_turn(state_dir):
    """Settling must not emit ``idle`` between two turns the user is waiting on."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id, _, _ = await c.pair()
            sess = hub.sessions[device_id]
            sess.screen = "thinking"
            launched: list[object] = []

            def queued(gen):
                launched.append(gen)
                return asyncio.sleep(0)

            sess.pending_turns.append(queued)
            await hub._settle(sess)

            # Nothing may arrive: the queued turn is about to speak for itself.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(c.recv(timeout=0.3), timeout=0.3)
            assert launched == [], "settle must not launch the queue"


async def test_settle_is_silent_when_the_screen_already_says_idle(state_dir):
    """The helper runs on every terminal path, so it must be free when nothing changed."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id, _, _ = await c.pair()
            sess = hub.sessions[device_id]
            sess.screen = "idle"
            await hub._settle(sess)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(c.recv(timeout=0.3), timeout=0.3)
