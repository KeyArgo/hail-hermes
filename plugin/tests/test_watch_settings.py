"""Watch-driven settings: the watch may read and change only what the owner allows.

Design rule: the watch never holds credentials. Its model choices are drawn from models Hermes
has already authorised, and the owner decides which five are offered.
"""

import pytest

from hail_hermes import models as models_api
from hail_hermes.settings import update_settings

from conftest import Client, running_hub

FAKE = [
    {"source": "sparks", "id": "GLM-5.3-Flash-EXL3", "label": "sparks · GLM", "tag": "subscription"},
    {"source": "lmstudio", "id": "qwen3-8b", "label": "lmstudio · qwen3-8b", "tag": "local"},
    {"source": "ollama", "id": "llama3.1:8b", "label": "ollama · llama3.1", "tag": "local"},
    {"source": "openrouter", "id": "free-model", "label": "openrouter · free", "tag": "subscription"},
    {"source": "anthropic", "id": "claude-fable-5.1", "label": "anthropic · claude", "tag": "subscription"},
    {"source": "anthropic", "id": "claude-fable-5", "label": "anthropic · claude-5", "tag": "subscription"},
]


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    monkeypatch.setattr(models_api, "available_models", lambda home=None: list(FAKE))
    yield


def _presets(*keys):
    return [k for k in keys]


async def test_settings_payload_respects_owner_switches(state_dir):
    update_settings(state_dir, {"watch_can_change": {"character": True, "model": False}})
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("get_settings", req="r1")
        msg = await c.expect("settings")
        assert msg["req"] == "r1"
        assert msg["can_change"]["character"] is True
        assert msg["can_change"]["model"] is False


async def test_presets_are_capped_at_five_and_only_authorised(state_dir):
    # Five slots is the hard cap on the watch, and one of them is a model Hermes never
    # authorised, so the watch must be offered four.
    update_settings(state_dir, {"watch_model_presets": [
        "sparks::GLM-5.3-Flash-EXL3",
        "lmstudio::qwen3-8b",
        "ollama::llama3.1:8b",
        "openrouter::free-model",
        "ghost::not-real",
    ]})
    with pytest.raises(ValueError):
        update_settings(state_dir, {"watch_model_presets": [f"sparks::m{i}" for i in range(6)]})

    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("list_models")
        msg = await c.expect("models")
        assert len(msg["items"]) == 4, "the ghost entry must be dropped, not offered"
        keys = {f"{m['source']}::{m['id']}" for m in msg["items"]}
        assert "ghost::not-real" not in keys, "an unauthorised model must never be offered"


async def test_watch_can_change_character_when_allowed(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("set_setting", req="r2", key="character", value="nous-teal")
        msg = await c.expect("settings_changed")
        assert msg["key"] == "character"
        assert msg["value"] == "nous-teal"
        from hail_hermes.settings import read_settings

        assert read_settings(state_dir)["character"] == "nous-teal"


async def test_watch_is_refused_when_the_owner_switched_it_off(state_dir):
    update_settings(state_dir, {"watch_can_change": {"character": False}})
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("set_setting", req="r3", key="character", value="hermes")
        msg = await c.expect("setting_error")
        assert msg["code"] == "not_allowed"


async def test_unknown_character_is_rejected(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("set_setting", req="r4", key="character", value="not-a-skin")
        msg = await c.expect("setting_error")
        assert msg["code"] == "bad_character"


async def test_model_change_only_accepts_a_preset(state_dir):
    update_settings(state_dir, {"watch_model_presets": ["sparks::GLM-5.3-Flash-EXL3"]})
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("set_setting", req="r5", key="model", value="anthropic::claude-fable-5")
        msg = await c.expect("setting_error")
        assert msg["code"] == "bad_value"

        await c.send("set_setting", req="r6", key="model", value="sparks::GLM-5.3-Flash-EXL3")
        ok = await c.expect("settings_changed")
        assert ok["provider"] == "sparks" and ok["model"] == "GLM-5.3-Flash-EXL3"


async def test_unknown_setting_is_a_named_error(state_dir):
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("set_setting", req="r7", key="lan_enabled", value=True)
        msg = await c.expect("setting_error")
        assert msg["code"] == "bad_setting"
