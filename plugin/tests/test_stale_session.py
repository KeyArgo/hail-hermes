"""A deleted session must not cost the user their question."""

from __future__ import annotations

import pytest

from hail_hermes.agent import AgentError, DeviceInfo, HermesCliAgent

DEVICE = DeviceInfo(device_id="dev-1", name="Watch", kind="wear", caps={})


def _agent() -> HermesCliAgent:
    return HermesCliAgent(binary="hermes", model="m", provider="p")


@pytest.mark.asyncio
async def test_a_missing_session_is_retried_as_a_new_one(monkeypatch):
    agent = _agent()
    calls: list[dict] = []
    stored: list[tuple[str, str]] = []

    async def fake_run_once(device, text, *, session_id=None, new_session=False, title=None):
        calls.append({"session_id": session_id, "new_session": new_session})
        if session_id:
            raise AgentError(
                "hermes run failed",
                detail="exit 1: No session found matching 'local-gone'. Use 'hermes sessions list'",
            )
        yield "answer"

    monkeypatch.setattr(agent, "_run_once", fake_run_once)
    monkeypatch.setattr(agent, "on_session", lambda device_id, sid: stored.append((device_id, sid)))

    out = [chunk async for chunk in agent.run(DEVICE, "hello", session_id="local-gone")]

    assert out == ["answer"]
    assert calls == [
        {"session_id": "local-gone", "new_session": False},
        {"session_id": None, "new_session": True},
    ]
    assert stored == [("dev-1", "")], "the stale id must be forgotten, not reused next turn"


@pytest.mark.asyncio
async def test_other_failures_still_raise(monkeypatch):
    agent = _agent()

    async def fake_run_once(device, text, *, session_id=None, new_session=False, title=None):
        raise AgentError("hermes run failed", detail="exit 2: model not available")
        yield  # pragma: no cover

    monkeypatch.setattr(agent, "_run_once", fake_run_once)

    with pytest.raises(AgentError):
        _ = [chunk async for chunk in agent.run(DEVICE, "hello", session_id="local-x")]


@pytest.mark.asyncio
async def test_a_session_pinned_to_a_dead_model_is_retried_as_a_fresh_one(monkeypatch):
    agent = _agent()
    calls: list[dict] = []
    stored: list[str] = []

    async def fake_run_once(device, text, *, session_id=None, new_session=False, title=None):
        calls.append({"session_id": session_id, "new_session": new_session})
        if session_id:
            raise AgentError(
                "hermes run failed",
                detail="exit 1: Model 'GLM-5.3-Flash-EXL3' isn't available on Nous Portal",
            )
        yield "answer"

    monkeypatch.setattr(agent, "_run_once", fake_run_once)
    monkeypatch.setattr(agent, "on_session", lambda device_id, sid: stored.append(sid))

    out = [chunk async for chunk in agent.run(DEVICE, "hello", session_id="local-poisoned")]

    assert out == ["answer"]
    assert calls == [
        {"session_id": "local-poisoned", "new_session": False},
        {"session_id": None, "new_session": True},
    ], "Hermes rejects model overrides on resume, so the only honest retry is a new session"
    assert stored == [""], "the poisoned session must not be reused"


@pytest.mark.asyncio
async def test_a_dead_model_is_caught_by_any_of_hermes_wording(monkeypatch):
    """Hermes has said this three different ways; the retry must not depend on the phrasing."""
    for wording in (
        "exit 1: Model 'X' isn't available on Nous Portal",
        "exit 1: HTTP 404: Model 'X' not found. The requested model does not exist in our configuration",
        "exit 1: No fallback model group found for original model_group=X",
    ):
        agent = _agent()
        calls: list[str] = []

        async def fake_run_once(device, text, *, session_id=None, new_session=False, title=None,
                                _w=wording):
            calls.append(session_id or "fresh")
            if session_id:
                raise AgentError("hermes run failed", detail=_w)
            yield "answer"

        monkeypatch.setattr(agent, "_run_once", fake_run_once)
        out = [chunk async for chunk in agent.run(DEVICE, "hi", session_id="local-dead")]
        assert out == ["answer"], wording
        assert calls == ["local-dead", "fresh"], wording


@pytest.mark.asyncio
async def test_a_session_with_foreign_launch_flags_is_retried_as_a_fresh_one(monkeypatch):
    """The exact failure that broke the owner's watch: a session created by a terminal run."""
    agent = _agent()
    calls: list[str] = []

    async def fake_run_once(device, text, *, session_id=None, new_session=False, title=None):
        calls.append(session_id or "fresh")
        if session_id:
            raise AgentError(
                "hermes run failed",
                detail="exit 2: Resume retains gateway session policy; creation overrides are "
                       "unsupported on resume. -t/--toolsets, --source differ from this "
                       "session's launch: resume without them, or start a new session to use them.",
            )
        yield "answer"

    monkeypatch.setattr(agent, "_run_once", fake_run_once)
    out = [chunk async for chunk in agent.run(DEVICE, "hi", session_id="local-cli")]
    assert out == ["answer"]
    assert calls == ["local-cli", "fresh"]
