"""An interrupt while the device is idle must not eat the answer that is landing.

Found on CI once the CI-only skips came off: `test_handoff_split_marker_speaks_on_it_and_delivers_result_later`
timed out on shared runners. Back-to-back `interrupt` and "background result is ready" is the ordering the
BACKLOG called "a user who interrupts the moment a background answer lands could see the same stall" — and
it was worse than a stall: the result's turn started with a generation the interrupt had just bumped, saw
itself as dead, and said nothing at all. No speech, no text, no `idle`, so the watch sat on "thinking"
forever.

`_interrupt` now cancels only a turn that is actually running; interrupting an idle session is a no-op
beyond the audit log.
"""

from __future__ import annotations

import asyncio

from conftest import Client, FakeAgent, is_type, running_hub
from hail_hermes.hub import INTERRUPTED_NOTE


class GatedAgent(FakeAgent):
    """Blocks until ``release`` is set, so a slow background answer can be timed deliberately."""

    def __init__(self, chunks=("Dentist at 4 PM.",)):
        super().__init__(chunks=chunks)
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, device, text, **kwargs):
        self.calls.append((device.device_id, text))
        self.started.set()
        await self.release.wait()
        try:
            for chunk in self.chunks:
                yield chunk
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def test_an_idle_interrupt_does_not_claim_the_next_turn_was_interrupted(state_dir):
    """The note is for a turn that was actually cut short, not for a tap on an idle screen."""
    agent = FakeAgent(chunks=("Four o'clock.",))
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            hub.store.set_device_mode(device_id, "session")

            await c.send("interrupt")
            await asyncio.sleep(0.1)

            await c.send("text", text="When is the dentist?")
            await c.until(is_type("state", screen="idle"), timeout=10)

    assert agent.calls, "the question never reached the agent"
    sent = agent.calls[-1][1]
    assert INTERRUPTED_NOTE not in sent, "an idle interrupt was reported to the agent as an interruption"


async def test_an_idle_interrupt_does_not_kill_the_session_generation(state_dir):
    """The generation is what every running turn checks to know whether it is still wanted."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            session = hub.sessions[device_id]
            before = session.generation

            await c.send("interrupt")
            await asyncio.sleep(0.1)

            assert session.generation == before, "an interrupt with nothing running bumped the generation"
            assert session.interrupted_pending is False


async def test_a_handoff_result_survives_an_interrupt_that_arrives_with_it(state_dir):
    """Interrupt, then deliver: the answer must still be spoken, and the screen must come back to idle."""
    agent = GatedAgent()
    async with running_hub(state_dir, agent=agent, quick_agent=None) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            hub.store.set_device_mode(device_id, "session")

            await c.send("text", text="When is the dentist?")
            await asyncio.wait_for(agent.started.wait(), 10)

            # Interrupt right as the answer is complete but before it has been spoken.
            await c.send("interrupt")
            agent.release.set()

            msgs = await c.until(is_type("state", screen="idle"), timeout=15)

    said = [m["text"] for m in msgs
            if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
    assert any("Dentist" in text for text in said), f"the answer was swallowed: {said}"
