"""Model discovery must never touch a credential store (catalog rule 11).

Hermes' catalog rules forbid a plugin from reading `.env` and `auth.json`, and say plainly that a
disclosure cannot make a prohibited read allowed. The hub used to parse both to work out which
providers the owner had authorised, which is how the panel could list 324 models. That is now gone:
discovery reads configuration, model *names* Hermes has cached, and unauthenticated local probes.

These tests exist because the old behaviour had none — every assertion below would have failed
against the code that read the credential files, and nothing else in the suite noticed when it left.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hail_hermes import models as models_api


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A profile that *has* credentials, so a reader would find them if one came back."""
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-should-never-be-read\n", encoding="utf-8")
    (tmp_path / "auth.json").write_text(
        json.dumps({"providers": {"nous": {"token": "tok-should-never-be-read",
                                          "base_url": "https://inference.example/v1"}}}),
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: openai\n  default: gpt-5.3-codex\n"
        "providers:\n  openai:\n    base_url: https://api.openai.example/v1\n    key_env: OPENAI_API_KEY\n"
        "  ollama:\n    base_url: http://127.0.0.1:11434/v1\n",
        encoding="utf-8",
    )
    (tmp_path / "provider_models_cache.json").write_text(
        json.dumps({"openai": {"models": [{"id": "gpt-5.3-codex"}]}}), encoding="utf-8"
    )
    return tmp_path


def test_the_credential_readers_are_gone() -> None:
    """A function that parses `.env`/`auth.json` must not come back by being imported elsewhere."""
    for name in ("_env_keys", "_oauth_entry", "_oauth_providers"):
        assert not hasattr(models_api, name), f"{name} reads a credential store"


def test_no_probe_is_ever_sent_a_key(monkeypatch, home: Path) -> None:
    """Probing a remote endpoint would need a key, so only local endpoints are probed at all."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(models_api, "probe_models", lambda url, key="", **kw: calls.append((url, key)) or [])
    models_api.available_models(home)
    assert all(key == "" for _, key in calls), calls
    assert all("127.0.0.1" in url or "localhost" in url for url, _ in calls), calls


def test_a_provider_with_a_key_on_disk_is_still_listed_but_not_claimed_as_authorised(home: Path) -> None:
    rows = models_api.available_models(home)
    openai = [r for r in rows if r["source"] == "openai"]
    assert openai, "the configured provider's cached model names should still be offered"
    assert all(r["verified"] == "unverified" for r in openai)
    # `authorized` was the old field name and the old claim; nothing may assert it any more.
    assert all("authorized" not in r for r in rows)


def test_the_configured_default_is_always_offered(home: Path) -> None:
    rows = models_api.available_models(home)
    assert any(r["source"] == "openai" and r["id"] == "gpt-5.3-codex" for r in rows)


def test_local_endpoints_are_marked_verified(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(models_api, "probe_models", lambda url, key="", **kw: ["qwen3:8b"])
    rows = models_api.available_models(home)
    local = [r for r in rows if r["source"] == "ollama"]
    assert local and all(r["verified"] == "local" for r in local)


def test_the_token_in_auth_json_is_never_used_as_a_probe_key(monkeypatch, home: Path) -> None:
    """The old code sent the Nous token as a bearer token. Nothing may carry it."""
    seen: list[str] = []

    def spy(url: str, key: str = "", **kw):
        seen.append(key)
        return []

    monkeypatch.setattr(models_api, "probe_models", spy)
    models_api.available_models(home)
    assert "tok-should-never-be-read" not in seen
    assert "sk-should-never-be-read" not in seen
