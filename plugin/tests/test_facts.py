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
