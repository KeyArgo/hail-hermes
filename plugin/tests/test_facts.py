"""Remembered facts live in a file this plugin owns; Hermes' MEMORY.md is left alone.

Review item 3. The plugin appended straight into core's `MEMORY.md`, which has an entry format, a size
budget and a safety scan that a direct append bypasses. These tests pin the replacement: a plugin-owned
facts file, read only by the quick backend and the pane's memory route, with core's file provably
untouched.

They fail on the reviewed tree — `append_fact` did not exist, and a "remember" grew `MEMORY.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import Client, is_type, running_hub
from hail_hermes import facts
from hail_hermes.facts import append_fact, facts_block, facts_path, read_facts


def _plugin_api():
    """Import the dashboard router with the host's FastAPI stubbed, like the API suite does.

    The plugin runs inside Hermes, which provides FastAPI; the test environment does not, and a stub is
    what the existing dashboard tests already use.
    """
    from test_dashboard_api import _stub_fastapi

    _stub_fastapi()
    from hail_hermes.dashboard import plugin_api

    return plugin_api


def test_facts_are_written_to_the_plugins_own_file(tmp_path):
    append_fact("I prefer dark mode", tmp_path)
    assert facts_path(tmp_path).is_file()
    assert read_facts(tmp_path) == ["I prefer dark mode"]


def test_core_memory_is_never_touched(tmp_path, monkeypatch):
    """The file the review named stays byte-identical even when a fact is remembered."""
    core = tmp_path / "profiles" / "work" / "memories" / "MEMORY.md"
    core.parent.mkdir(parents=True)
    original = "# Memories\n- do not touch\n"
    core.write_text(original, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "work"))

    append_fact("The garden tap is on the left", tmp_path)

    assert core.read_text(encoding="utf-8") == original
    assert "garden tap" not in core.read_text(encoding="utf-8")


def test_a_duplicate_fact_is_not_stored_twice(tmp_path):
    append_fact("I prefer dark mode", tmp_path)
    append_fact("i prefer DARK mode", tmp_path)
    assert read_facts(tmp_path) == ["I prefer dark mode"]


def test_facts_are_capped_and_empty_ones_refused(tmp_path):
    append_fact("x" * 500, tmp_path)
    assert read_facts(tmp_path) == ["x" * facts.MAX_FACT_CHARS]
    with pytest.raises(ValueError):
        append_fact("   ", tmp_path)


def test_the_oldest_facts_are_trimmed(tmp_path):
    for i in range(facts.MAX_FACTS + 5):
        append_fact(f"fact {i}", tmp_path)
    stored = read_facts(tmp_path)
    assert len(stored) == facts.MAX_FACTS
    assert stored[-1] == f"fact {facts.MAX_FACTS + 4}"
    assert "fact 0" not in stored


def test_the_prompt_block_names_the_source_honestly(tmp_path):
    assert facts_block(tmp_path) == ""
    append_fact("I prefer dark mode", tmp_path)
    block = facts_block(tmp_path)
    assert block.startswith("Facts the owner asked you to remember:")
    assert "- I prefer dark mode" in block


def _core_memory(tmp_path, monkeypatch, text="- the agent knows this\n"):
    """A real Hermes profile with its own MEMORY.md, the file this plugin must never write."""
    core = tmp_path / "profiles" / "work" / "memories" / "MEMORY.md"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "work"))
    return core


def test_the_pane_memory_route_reads_the_plugin_file(tmp_path, monkeypatch):
    """`GET /memory` feeds the panel; it must show these facts and not core's file."""
    plugin_api = _plugin_api()

    append_fact("I prefer dark mode", tmp_path)
    monkeypatch.setattr(plugin_api, "_resolve_state_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(plugin_api, "_run_bounded", lambda fn, *a, **k: fn(*a))

    body = plugin_api.get_memory()
    assert body["ok"] is True
    assert body["lines"] == ["- I prefer dark mode"]
    assert body["path"] == str(facts_path(tmp_path))
    assert "MEMORY.md" not in body["path"]


def test_the_pane_can_write_a_fact(tmp_path, monkeypatch):
    plugin_api = _plugin_api()

    monkeypatch.setattr(plugin_api, "_resolve_state_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(plugin_api, "_run_bounded", lambda fn, *a, **k: fn(*a))

    body = plugin_api.post_memory({"fact": "I prefer dark mode"})
    assert body["ok"] is True and body["saved"] is True
    assert read_facts(tmp_path) == ["I prefer dark mode"]


def test_the_dashboard_route_leaves_core_memory_alone(tmp_path, monkeypatch):
    """The panel's own route: write through it, then look at both files."""
    plugin_api = _plugin_api()
    core = _core_memory(tmp_path, monkeypatch)
    before = core.read_bytes()

    monkeypatch.setattr(plugin_api, "_resolve_state_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(plugin_api, "_run_bounded", lambda fn, *a, **k: fn(*a))

    body = plugin_api.post_memory({"fact": "The studio is upstairs"})

    assert body["ok"] is True
    assert read_facts(tmp_path) == ["The studio is upstairs"]
    assert core.read_bytes() == before, "the dashboard route wrote to Hermes' MEMORY.md"
    assert plugin_api.get_memory()["lines"] == ["- The studio is upstairs"]


class _Quick:
    """Minimal quick backend: the hub only needs something that can stream an answer."""

    async def run(self, device, text):
        yield "ok"


async def test_the_voice_route_leaves_core_memory_alone(state_dir, tmp_path, monkeypatch):
    """The watch's path: a quick-mode "remember …" over the socket."""
    core = _core_memory(tmp_path, monkeypatch)
    before = core.read_bytes()

    async with running_hub(state_dir, quick_agent=_Quick()) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            hub.store.set_device_mode(device_id, "quick")
            await c.send("text", text="Remember that I prefer dark mode.")
            await c.until(is_type("state", screen="idle"), timeout=5)

    assert read_facts(state_dir) == ["I prefer dark mode"]
    assert core.read_bytes() == before, "a voice command wrote to Hermes' MEMORY.md"


async def test_the_voice_route_in_a_session_does_not_touch_either_file(state_dir, tmp_path, monkeypatch):
    """A session's memory belongs to the agent; the plugin writes nothing at all."""
    core = _core_memory(tmp_path, monkeypatch)
    before = core.read_bytes()

    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            device_id, _token, _welcome = await c.pair()
            hub.store.set_device_mode(device_id, "session")
            await c.send("text", text="Remember that I prefer dark mode.")
            await c.until(is_type("state", screen="idle"), timeout=5)

    assert read_facts(state_dir) == [], "session mode must not write the plugin's facts"
    assert core.read_bytes() == before
