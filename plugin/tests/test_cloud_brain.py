import asyncio
import sys
import threading
import types

import pytest

from hail_hermes.agent import DeviceInfo, HermesCliAgent
from hail_hermes.quick import HermesModelBackend, QuickAgent
from hail_hermes.settings import read_settings, update_settings


def response(content, field="delta"):
    return {"choices": [{field: {"content": content}}]}


def install_client(monkeypatch, call):
    package = types.ModuleType("agent")
    package.__path__ = []
    module = types.ModuleType("agent.auxiliary_client")
    module.call_llm = call
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)


def test_streaming(monkeypatch):
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        return iter([response("Hello"), response(" there")])
    install_client(monkeypatch, call)
    messages = [{"role": "user", "content": "Hi"}]
    assert list(HermesModelBackend().stream(messages, threading.Event())) == ["Hello", " there"]
    assert calls == [{"messages": messages, "stream": True}]


def test_fallback_before_first_delta(monkeypatch):
    calls = []
    def call(**kwargs):
        calls.append(kwargs["stream"])
        if kwargs["stream"]:
            def broken():
                raise RuntimeError("stream failed")
                yield
            return broken()
        return response("Recovered", "message")
    install_client(monkeypatch, call)
    assert list(HermesModelBackend().stream([], threading.Event())) == ["Recovered"]
    assert calls == [True, False]


def test_no_retry_after_delta(monkeypatch):
    calls = []
    def call(**kwargs):
        calls.append(kwargs["stream"])
        def broken():
            yield response("Partial")
            raise RuntimeError("stream failed")
        return broken()
    install_client(monkeypatch, call)
    stream = HermesModelBackend().stream([], threading.Event())
    assert next(stream) == "Partial"
    with pytest.raises(RuntimeError, match="stream failed"):
        next(stream)
    assert calls == [True]


def test_quick_agent_uses_hermes(monkeypatch, tmp_path):
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        return iter([response("Hi!"), response(" Welcome.")])
    install_client(monkeypatch, call)
    quick = QuickAgent("", "", state_dir=tmp_path)
    async def collect():
        return [part async for part in quick.run(DeviceInfo("watch"), "Hello")]
    assert asyncio.run(collect()) == ["Hi!", " Welcome."]
    assert calls[0]["messages"][-1] == {"role": "user", "content": "Hello"}
    assert quick.history("watch")[-1]["content"] == "Hi! Welcome."


def test_named_session_argv():
    agent = HermesCliAgent(binary="hermes", session_lookup=lambda _: "old-id")
    device = DeviceInfo("watch")
    argv = agent.argv(device, new_session=True, session_title="Hail Hermes · Hermes")
    index = argv.index("-c")
    assert argv[index:index + 3] == ["-c", "Hail Hermes · Hermes", "--create-if-missing"]
    resumed = agent.argv(device, session_id="explicit", session_title="ignored")
    assert resumed[resumed.index("--resume") + 1] == "explicit"
    assert "-c" not in resumed


def test_quick_backend_setting(tmp_path):
    assert read_settings(tmp_path)["quick_backend"] == "hermes"
    update_settings(tmp_path, {"quick_url": "http://localhost:8000"})
    assert read_settings(tmp_path)["quick_backend"] == "url"
    update_settings(tmp_path, {"quick_url": ""})
    assert read_settings(tmp_path)["quick_backend"] == "hermes"
