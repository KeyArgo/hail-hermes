"""Tests for named slots, voice commands, memory appending, and QuickAgent memory context."""

from __future__ import annotations

from test_dashboard_api import _stub_fastapi

_stub_fastapi()

import os
from pathlib import Path

from conftest import Client, FakeAgent, is_type, running_hub
from hail_hermes import filelock
from hail_hermes.hub import match_slot
from hail_hermes.quick import QuickAgent, append_memory, resolve_memory_path
from hail_hermes.settings import read_settings, update_settings


class SlotRecordingAgent:
    """Fake agent that records session_id, new_session, and title arguments."""

    def __init__(self, next_sid: str = "created-sid-1"):
        self.calls: list[dict] = []
        self.next_sid = next_sid
        self.last_session_id: str | None = None

    async def run(self, device, text, *, session_id=None, new_session=False, title=None):
        self.calls.append(
            {
                "device_id": device.device_id,
                "text": text,
                "session_id": session_id,
                "new_session": new_session,
                "title": title,
            }
        )
        if new_session and self.next_sid:
            self.last_session_id = self.next_sid
        yield f"Handled: {text}"


def test_fuzzy_slot_matching():
    slots = [
        {"id": "s1", "name": "Work", "title": "Work"},
        {"id": "s2", "name": "Research Project", "title": "Research Project"},
        {"id": "s3", "name": "Fitness", "title": "Fitness"},
    ]
    # Exact match (case-insensitive)
    assert match_slot("work", slots)["name"] == "Work"
    # Startswith match
    assert match_slot("research", slots)["name"] == "Research Project"
    # Difflib ratio >= 0.75 ("fitnes" vs "fitness" = 0.923)
    assert match_slot("fitnes", slots)["name"] == "Fitness"
    # Below 0.75 returns None
    assert match_slot("completely unknown", slots) is None


async def test_voice_commands_switch_quick_where_and_unknown(state_dir):
    update_settings(
        state_dir,
        {
            "slots": [
                {"id": "sid-work", "name": "Work", "title": "Work"},
                {"id": "sid-home", "name": "Home", "title": "Home"},
            ]
        },
    )
    agent = FakeAgent()
    quick = FakeAgent(chunks=("Quick reply.",))
    async with running_hub(state_dir, agent=agent, quick_agent=quick) as hub:
        async with Client.open(hub) as c:
            await c.pair(modes=True)
            await c.expect("mode")
            await c.expect("slots")

            # Switch to Work
            await c.send("text", text="Switch to Work!")
            msgs = await c.until(is_type("state", screen="idle"))
            mode_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "mode"]
            slot_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "slots"]
            assist = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
            assert mode_msgs and mode_msgs[-1]["mode"] == "session"
            assert mode_msgs[-1]["session_id"] == "sid-work"
            assert slot_msgs
            assert assist[-1]["text"] == "Switched to Work."
            assert not agent.calls

            # Where am I (session mode)
            await c.send("text", text="Which session am I in?")
            msgs = await c.until(is_type("state", screen="idle"))
            assist = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
            assert "session" in assist[-1]["text"].lower()
            assert "Work" in assist[-1]["text"]

            # Unknown slot lists available names
            await c.send("text", text="go to Nonexistent")
            msgs = await c.until(is_type("state", screen="idle"))
            assist = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
            assert "Work" in assist[-1]["text"] and "Home" in assist[-1]["text"]

            # Open <name> session
            await c.send("text", text="open home session")
            msgs = await c.until(is_type("state", screen="idle"))
            assist = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
            assert assist[-1]["text"] == "Switched to Home."

            # Back to quick ask
            await c.send("text", text="back to quick ask")
            msgs = await c.until(is_type("state", screen="idle"))
            mode_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "mode"]
            assert mode_msgs and mode_msgs[-1]["mode"] == "quick"

            # Where am I (quick mode)
            await c.send("text", text="where am I?")
            msgs = await c.until(is_type("state", screen="idle"))
            assist = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
            assert "quick" in assist[-1]["text"].lower()


async def test_new_session_called_and_lazy_slot_creation(state_dir):
    update_settings(
        state_dir,
        {
            "slots": [
                {"id": "s1", "name": "Alpha"},
                {"id": "s2", "name": "Beta"},
                {"id": "s3", "name": "Gamma"},
            ]
        },
    )
    agent = SlotRecordingAgent(next_sid="sid-delta-99")
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            await c.pair(modes=True)
            await c.expect("mode")
            await c.expect("slots")

            # Creating a 4th slot replaces the oldest ("Alpha")
            await c.send("text", text="New session called Delta.")
            msgs = await c.until(is_type("state", screen="idle"))
            slot_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "slots"]
            assert slot_msgs
            names = [it["name"] for it in slot_msgs[-1]["items"]]
            assert names == ["Beta", "Gamma", "Delta"]
            assert slot_msgs[-1]["items"][-1]["id"] == ""
            assert not agent.calls

            # First real utterance in Delta creates the named Hermes session.
            await c.send("text", text="Review the deployment checklist")
            await c.until(is_type("state", screen="idle"))
            assert len(agent.calls) == 1
            assert agent.calls[0]["new_session"] is True
            assert agent.calls[0]["session_id"] is None
            assert agent.calls[0]["title"] == "Hail Hermes · Delta"

            # Slot now persists the created session_id and resumes it on the next turn
            saved_slots = read_settings(state_dir)["slots"]
            assert saved_slots[-1]["id"] == "sid-delta-99"

            await c.send("text", text="Next step please")
            await c.until(is_type("state", screen="idle"))
            assert len(agent.calls) == 2
            assert agent.calls[1]["session_id"] == "sid-delta-99"
            assert agent.calls[1]["new_session"] is False


async def test_remember_voice_command_and_memory_append_lock(state_dir, tmp_path, monkeypatch):
    mem_file = tmp_path / "profiles" / "work" / "memories" / "MEMORY.md"
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    initial = "# Existing Memories\n- Initial entry\n"
    mem_file.write_text(initial, encoding="utf-8")
    initial_ino = mem_file.stat().st_ino

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "work"))
    assert resolve_memory_path(state_dir) == mem_file

    lock_calls: list[tuple[int, int]] = []
    real_lock = filelock.lock

    def tracked_lock(target):
        fd = target if isinstance(target, int) else target.fileno()
        lock_calls.append((fd, 1))
        return real_lock(target)

    monkeypatch.setattr(filelock, "lock", tracked_lock)

    agent = FakeAgent()
    async with running_hub(state_dir, agent=agent) as hub:
        async with Client.open(hub) as c:
            await c.pair()
            await c.send("text", text="Remember that I prefer dark mode.")
            msgs = await c.until(is_type("state", screen="idle"))
            assist = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"]
            assert assist[-1]["text"] == "Got it, I'll remember that."
            assert not agent.calls

            long_fact = "x" * 450
            await c.send("text", text=f"Remember {long_fact}")
            await c.until(is_type("state", screen="idle"))

    # File was appended in place (same inode, existing content preserved, 300-char cap)
    assert mem_file.stat().st_ino == initial_ino
    assert lock_calls
    content = mem_file.read_text(encoding="utf-8")
    assert content.startswith(initial)
    lines = content.strip().splitlines()
    assert lines[2] == "- I prefer dark mode"
    assert lines[3] == f"- {'x' * 300}"


def test_quick_agent_prompt_includes_memories_and_reloads_on_mtime(state_dir, tmp_path):
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"
    soul_file = state_dir / "soul.md"

    update_settings(state_dir, {"memory_path": str(mem_file)})

    mem_file.write_text("DROP_ME_" + ("M" * 2000), encoding="utf-8")
    user_file.write_text("USER_PREFIX_" + ("U" * 2000), encoding="utf-8")

    qa = QuickAgent("http://127.0.0.1:9/v1", "test-model", soul_path=soul_file, state_dir=state_dir)
    prompt1 = qa.system_prompt
    assert qa._soul_text in prompt1
    assert ("M" * 2000) in prompt1
    assert "DROP_ME_" not in prompt1
    assert ("U" * 2000) in prompt1
    assert "USER_PREFIX_" not in prompt1

    # Updating MEMORY.md and advancing its mtime triggers a re-read
    append_memory("User loves espresso", state_dir=state_dir)
    st = mem_file.stat()
    os.utime(mem_file, (st.st_atime, st.st_mtime + 2.0))

    prompt2 = qa.system_prompt
    assert "User loves espresso" in prompt2
