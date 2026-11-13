"""Quick mode still knows what Hermes already knows.

Quality review: when the plugin stopped writing to Hermes' `MEMORY.md`, quick mode's prompt stopped
*reading* it too, so the first quick answer after an upgrade knew nothing the owner had told Hermes in
months — a silent loss with no notice. The fix is to read it, not to write it: the agent still owns that
file, and these tests prove the plugin only ever looks.

Real `QuickAgent` objects, real files, real prompt construction — the prompt is what the model sees, so it
is the thing worth asserting on.
"""

from __future__ import annotations

import os

import pytest

from hail_hermes.facts import append_fact
from hail_hermes.quick import QuickAgent


def _agent(state_dir, tmp_path):
    return QuickAgent("http://127.0.0.1:9/v1", "test-model",
                      soul_path=tmp_path / "soul.md", state_dir=state_dir)


def _core_memory(tmp_path, monkeypatch, text: str):
    """A Hermes profile with its own MEMORY.md, the way an existing install has one."""
    memories = tmp_path / "profiles" / "work" / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    path = memories / "MEMORY.md"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "work"))
    return path


def test_an_upgraded_install_keeps_the_memory_it_already_had(tmp_path, monkeypatch):
    core = _core_memory(tmp_path, monkeypatch, "# Memories\n- The owner's studio is upstairs\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    prompt = _agent(state_dir, tmp_path).system_prompt

    assert "The owner's studio is upstairs" in prompt, "quick mode forgot what Hermes already knew"
    assert "read-only" in prompt, "the prompt must not imply the plugin will write to that file"


def test_reading_memory_never_writes_to_it(tmp_path, monkeypatch):
    core = _core_memory(tmp_path, monkeypatch, "# Memories\n- do not touch\n")
    before = core.read_bytes()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    agent = _agent(state_dir, tmp_path)
    _ = agent.system_prompt
    append_fact("I prefer dark mode", state_dir)
    _ = agent.system_prompt

    assert core.read_bytes() == before
    assert "I prefer dark mode" in agent.system_prompt


def test_both_memories_reach_the_prompt(tmp_path, monkeypatch):
    _core_memory(tmp_path, monkeypatch, "- Studio is upstairs\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    append_fact("I prefer dark mode", state_dir)

    prompt = _agent(state_dir, tmp_path).system_prompt

    assert "Studio is upstairs" in prompt
    assert "I prefer dark mode" in prompt
    assert prompt.index("Studio is upstairs") < prompt.index("I prefer dark mode"), (
        "Hermes' own memory should come before this plugin's facts"
    )


def test_the_old_memory_path_setting_is_gone_and_cannot_be_set(tmp_path):
    """It pointed at a file this plugin no longer writes, so it is not a setting any more."""
    from hail_hermes.settings import update_settings

    with pytest.raises(ValueError):
        update_settings(tmp_path, {"memory_path": str(tmp_path / "MEMORY.md")})


def test_a_new_memory_entry_is_picked_up_without_a_restart(tmp_path, monkeypatch):
    core = _core_memory(tmp_path, monkeypatch, "- first entry\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    agent = _agent(state_dir, tmp_path)
    assert "first entry" in agent.system_prompt

    core.write_text("- first entry\n- second entry\n", encoding="utf-8")
    stat = core.stat()
    os.utime(core, (stat.st_atime, stat.st_mtime + 2.0))

    assert "second entry" in agent.system_prompt
