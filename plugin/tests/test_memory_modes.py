"""Which memory a spoken "remember …" reaches.

Quality review, blocker: the hub intercepted "remember …" in *every* mode and wrote it to the plugin's
facts file, which only quick mode reads. In a session the agent — which has its own memory toolset, with an
entry format, a size budget and a safety scan — never saw the sentence at all, so the thing the README tells
people to do ("ask the agent to remember something") silently did nothing.

These drive the hub over its WebSocket the way a watch does: one session-mode turn and one quick-mode turn.
On the reviewed tree both would answer "Got it, I'll remember that." and leave the agent with nothing.
"""

from __future__ import annotations

import asyncio

from conftest import Client, FakeAgent, is_type, running_hub
from hail_hermes.facts import facts_path, read_facts

UTTERANCE = "Remember that I prefer dark mode."


class RecordingQuick:
    """Stand-in for the quick backend: records what it was asked, answers in one chunk."""

    def __init__(self, answer: str = "Quick answer."):
        self.calls: list[str] = []
        self.answer = answer

    async def run(self, device, text):
        self.calls.append(text)
        yield self.answer


def _assistant_text(msgs) -> str:
    said = [m for m in msgs
            if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
    return said[-1]["text"] if said else ""


async def test_session_mode_gives_the_sentence_to_the_agent(state_dir):
    agent = FakeAgent(chunks=("Noted in your memory.",))
    quick = RecordingQuick()
    async with running_hub(state_dir, agent=agent, quick_agent=quick) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            # A device with a quick backend configured starts in quick mode; this test is about the
            # other path, so put it in a session the way the pane's session picker does.
            hub.store.set_device_mode(device_id, "session")
            assert hub._effective_mode(device_id)[0] == "session"

            await c.send("text", text=UTTERANCE)
            msgs = await c.until(is_type("state", screen="idle"))

    assert agent.calls, "the agent never received a 'remember …' in a session"
    assert agent.calls[-1][1] == UTTERANCE, f"the agent got {agent.calls[-1][1]!r}"
    assert read_facts(state_dir) == [], "a session's memory must not land in the quick-mode facts file"
    assert _assistant_text(msgs) == "Noted in your memory."


async def test_quick_mode_stores_the_fact_without_spending_a_model_turn(state_dir):
    agent = FakeAgent()
    quick = RecordingQuick()
    async with running_hub(state_dir, agent=agent, quick_agent=quick) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            hub.store.set_device_mode(device_id, "quick")
            assert hub._effective_mode(device_id)[0] == "quick"

            await c.send("text", text=UTTERANCE)
            msgs = await c.until(is_type("state", screen="idle"))

    assert read_facts(state_dir) == ["I prefer dark mode"], read_facts(state_dir)
    assert not quick.calls, "quick mode must answer a remembered fact locally"
    assert not agent.calls, "a remembered fact is stored locally; it must not spend a session turn"
    assert _assistant_text(msgs) == "Got it, I'll remember that."


async def test_a_session_can_switch_to_quick_and_then_be_remembered_locally(state_dir):
    """The two paths are one switch apart, so both must be reachable in one conversation."""
    agent = FakeAgent()
    quick = RecordingQuick()
    async with running_hub(state_dir, agent=agent, quick_agent=quick) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            hub.store.set_device_mode(device_id, "session")
            assert hub._effective_mode(device_id)[0] == "session"

            await c.send("text", text=UTTERANCE)
            await c.until(is_type("state", screen="idle"))
            assert agent.calls, "session mode must reach the agent"

            hub.store.set_device_mode(device_id, "quick")
            await c.send("text", text=UTTERANCE)
            await c.until(is_type("state", screen="idle"))

    assert read_facts(state_dir) == ["I prefer dark mode"]
    assert len(agent.calls) == 1, "the quick-mode turn must not reach the session agent"
