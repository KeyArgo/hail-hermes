"""Tests for Hail Hermes plugin-side control, models, slots, soul.md, goodbye, and STT threads."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from websockets.asyncio.client import connect

from hail_hermes import cli, parakeet_worker
from hail_hermes.agent import DeviceInfo
from test_dashboard_api import _stub_fastapi

_stub_fastapi()

from hail_hermes.dashboard import plugin_api
from hail_hermes.hub import HubConfig, PocketHub, is_goodbye
from hail_hermes.quick import SYSTEM_PROMPT, QuickAgent, ensure_soul_file
from hail_hermes.settings import DEFAULTS, character_label, read_settings, update_settings, validate
from hail_hermes.store import Store


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    models: list[str] = ["qwen2.5-1.5b", "llama3.2-1b"]
    last_chat_body: dict[str, Any] | None = None
    reply_text: str = "Hello there friend!"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            payload = {
                "object": "list",
                "data": [{"id": m, "object": "model"} for m in self.models],
            }
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            type(self).last_chat_body = json.loads(raw.decode("utf-8"))
            chunk = json.dumps(
                {"choices": [{"delta": {"content": self.reply_text}}]}
            ).encode("utf-8")
            body = b"data: " + chunk + b"\n\ndata: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


@pytest.fixture
def fake_openai_server():
    _FakeOpenAIHandler.models = ["qwen2.5-1.5b", "llama3.2-1b"]
    _FakeOpenAIHandler.last_chat_body = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


class _FakeSessionAgent:
    def __init__(self) -> None:
        self.sessions = [
            {"id": "sess-1", "title": "Morning plan", "age": "5m"},
            {"id": "sess-2", "title": "Grocery list", "age": "1h"},
            {"id": "sess-3", "title": "Workout log", "age": "2h"},
        ]
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        device: DeviceInfo,
        text: str,
        *,
        session_id: str | None = None,
        new_session: bool = False,
        title: str | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "device_id": device.device_id,
                "text": text,
                "session_id": session_id,
                "new_session": new_session,
                "title": title,
            }
        )
        yield f"ack:{text}"


def test_settings_defaults_and_validation(tmp_path: Path) -> None:
    assert "quick_url" in DEFAULTS
    assert "quick_model" in DEFAULTS
    assert DEFAULTS["voice_session"] == "new"
    assert DEFAULTS["pinned_session_id"] == ""
    assert DEFAULTS["slots"] == []
    assert DEFAULTS["stt_threads"] == (os.cpu_count() or 4)
    assert DEFAULTS["soul_path"].endswith("soul.md")

    loaded = read_settings(tmp_path)
    assert loaded["soul_path"] == str(tmp_path / "soul.md")
    assert loaded["voice_session"] == "new"
    assert loaded["slots"] == []

    valid = validate(
        {
            "voice_session": "pinned",
            "pinned_session_id": "sess-123",
            "slots": [{"id": "s1", "title": "First"}, {"id": "s2", "title": "Second"}],
            "stt_threads": 6,
            "soul_path": str(tmp_path / "custom_soul.md"),
            "quick_endpoints": ["http://127.0.0.1:11434"],
        }
    )
    assert valid["voice_session"] == "pinned"
    assert valid["stt_threads"] == 6
    assert len(valid["slots"]) == 2

    with pytest.raises(ValueError):
        validate({"voice_session": "invalid"})
    with pytest.raises(ValueError):
        validate({"stt_threads": 0})
    with pytest.raises(ValueError):
        validate({"stt_threads": True})
    with pytest.raises(ValueError):
        validate(
            {
                "slots": [
                    {"id": "1", "title": "a"},
                    {"id": "2", "title": "b"},
                    {"id": "3", "title": "c"},
                    {"id": "4", "title": "d"},
                ]
            }
        )
    with pytest.raises(ValueError):
        validate({"slots": [{"id": "", "title": "bad"}]})


def test_models_picker_and_settings_guard(
    tmp_path: Path, fake_openai_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin_api, "_resolve_state_dir", lambda: str(tmp_path))
    update_settings(
        tmp_path,
        {"quick_endpoints": [fake_openai_server, "http://127.0.0.1:9"]},
    )

    listing = plugin_api.get_models()
    assert list(listing) == [
        {"url": fake_openai_server, "model": "qwen2.5-1.5b"},
        {"url": fake_openai_server, "model": "llama3.2-1b"},
    ]

    ok_res = plugin_api.post_settings(
        {"quick_url": fake_openai_server, "quick_model": "qwen2.5-1.5b"}
    )
    assert ok_res["ok"] is True
    assert read_settings(tmp_path)["quick_model"] == "qwen2.5-1.5b"

    bad_res = plugin_api.post_settings(
        {"quick_url": fake_openai_server, "quick_model": "nonexistent-model"}
    )
    assert bad_res["ok"] is False
    assert read_settings(tmp_path)["quick_model"] == "qwen2.5-1.5b"

    # A model added after the last listing is accepted if reachable now.
    _FakeOpenAIHandler.models = ["qwen2.5-1.5b", "live-new-model"]
    live_res = plugin_api.post_settings(
        {"quick_url": fake_openai_server, "quick_model": "live-new-model"}
    )
    assert live_res["ok"] is True
    assert read_settings(tmp_path)["quick_model"] == "live-new-model"

    assert cli.main(["--state-dir", str(tmp_path), "models"]) == 0


@pytest.mark.asyncio
async def test_slots_message_routes_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin_api, "_resolve_state_dir", lambda: str(tmp_path))
    store = Store(tmp_path)
    agent = _FakeSessionAgent()
    token = store.issue_token("watch-1", "Watch", "wear")

    async def fake_list_sessions(limit: int = 30, **kwargs: Any) -> list[dict[str, str]]:
        return agent.sessions[:limit]

    monkeypatch.setattr(plugin_api, "list_hermes_sessions", fake_list_sessions)

    async with PocketHub(
        agent=agent, store=store, config=HubConfig(port=0, audio_pacing=False)
    ) as hub:
        async with connect(hub.url) as ws:
            await ws.send(
                json.dumps(
                    {
                        "v": 2,
                        "type": "hello",
                        "device_id": "watch-1",
                        "name": "Watch",
                        "kind": "wear",
                        "caps": {},
                        "token": token,
                        "modes": True,
                    }
                )
            )
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            mode_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            slots_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            character_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert welcome["type"] == "welcome"
            assert mode_msg["type"] == "mode"
            assert slots_msg == {"v": 2, "type": "slots", "items": []}
            assert character_msg["type"] == "character"

            res = await asyncio.to_thread(plugin_api.post_slots, {"slots": ["sess-1", "sess-2"]})
            assert res["ok"] is True
            assert res["slots"] == [
                {"id": "sess-1", "name": "", "title": "Morning plan"},
                {"id": "sess-2", "name": "", "title": "Grocery list"},
            ]

            updated = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert updated == {
                "v": 2,
                "type": "slots",
                "items": [
                    {"slot": 1, "id": "sess-1", "name": "Morning plan", "title": "Morning plan"},
                    {"slot": 2, "id": "sess-2", "name": "Grocery list", "title": "Grocery list"},
                ],
            }

            assert await asyncio.to_thread(cli.main, ["--state-dir", str(tmp_path), "slots", "set", "3", "sess-3"]) == 0
            while True:
                after_set = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                if len(after_set.get("items", [])) == 3:
                    break
            assert len(after_set["items"]) == 3
            assert after_set["items"][2] == {
                "slot": 3,
                "id": "sess-3",
                "name": "Workout log",
                "title": "Workout log",
            }

            assert await asyncio.to_thread(cli.main, ["--state-dir", str(tmp_path), "slots", "clear", "2"]) == 0
            while True:
                after_clear = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                if [it["id"] for it in after_clear.get("items", [])] == ["sess-1", "sess-3"]:
                    break
            assert [it["id"] for it in after_clear["items"]] == ["sess-1", "sess-3"]


@pytest.mark.asyncio
async def test_soul_default_copy_and_mtime_reload(
    tmp_path: Path, fake_openai_server: str
) -> None:
    soul_file = tmp_path / "soul.md"
    assert not soul_file.exists()

    ensure_soul_file(soul_file)
    assert soul_file.is_file()
    default_text = soul_file.read_text(encoding="utf-8").strip()
    assert default_text

    agent = QuickAgent(fake_openai_server, "qwen2.5-1.5b", soul_path=soul_file)
    assert default_text in agent.system_prompt
    assert SYSTEM_PROMPT in agent.system_prompt
    assert "[[HERMES]]" in agent.system_prompt

    device = DeviceInfo(device_id="d1", name="Watch", kind="wear", caps={})
    chunks = [c async for c in agent.run(device, "hello")]
    assert "".join(chunks) == "Hello there friend!"
    assert _FakeOpenAIHandler.last_chat_body is not None
    sys_msg_1 = _FakeOpenAIHandler.last_chat_body["messages"][0]["content"]
    assert default_text in sys_msg_1

    custom_soul = "You are Captain Hermes, a cheerful sea navigator."
    soul_file.write_text(custom_soul + "\n", encoding="utf-8")
    future_mtime = time.time() + 10.0
    os.utime(soul_file, (future_mtime, future_mtime))

    _ = [c async for c in agent.run(device, "ahoy")]
    sys_msg_2 = _FakeOpenAIHandler.last_chat_body["messages"][0]["content"]
    assert custom_soul in sys_msg_2
    assert SYSTEM_PROMPT in sys_msg_2


@pytest.mark.asyncio
async def test_goodbye_detection_and_conversation_end(tmp_path: Path) -> None:
    for phrase in (
        "goodbye",
        "Bye!",
        "That's all.",
        "stop listening",
        "Thanks, that's it!",
    ):
        assert is_goodbye(phrase)
    assert not is_goodbye("tell me about goodbye yellow brick road")

    store = Store(tmp_path)
    agent = _FakeSessionAgent()
    token = store.issue_token("watch-bye", "Watch", "wear")

    async with PocketHub(
        agent=agent, store=store, config=HubConfig(port=0, audio_pacing=False)
    ) as hub:
        async with connect(hub.url) as ws:
            await ws.send(
                json.dumps(
                    {
                        "v": 2,
                        "type": "hello",
                        "device_id": "watch-bye",
                        "name": "Watch",
                        "kind": "wear",
                        "caps": {},
                        "token": token,
                    }
                )
            )
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert welcome["type"] == "welcome"

            await ws.send(json.dumps({"v": 2, "type": "text", "text": "thanks that's it"}))
            events: list[dict[str, Any]] = []
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                events.append(msg)
                if msg.get("type") == "state" and msg.get("screen") == "idle":
                    break

            assert agent.calls == []
            assistant_finals = [
                e
                for e in events
                if e.get("type") == "transcript"
                and e.get("role") == "assistant"
                and e.get("final") is True
            ]
            assert len(assistant_finals) == 1
            assert "bye" in assistant_finals[0]["text"].lower()
            assert events[-1] == {
                "v": 2,
                "type": "state",
                "screen": "idle",
                "conversation_end": True,
            }


@pytest.mark.asyncio
async def test_voice_session_new_and_pinned(tmp_path: Path) -> None:
    store = Store(tmp_path)
    agent = _FakeSessionAgent()
    token = store.issue_token("watch-vs", "Watch", "wear")

    async with PocketHub(
        agent=agent, store=store, config=HubConfig(port=0, audio_pacing=False)
    ) as hub:
        async with connect(hub.url) as ws:
            await ws.send(
                json.dumps(
                    {
                        "v": 2,
                        "type": "hello",
                        "device_id": "watch-vs",
                        "name": "Watch",
                        "kind": "wear",
                        "caps": {},
                        "token": token,
                    }
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)  # welcome

            await ws.send(json.dumps({"v": 2, "type": "text", "text": "draft a haiku about the sea"}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                if msg.get("type") == "state" and msg.get("screen") == "idle":
                    break

            assert len(agent.calls) == 1
            assert agent.calls[0]["new_session"] is True
            assert agent.calls[0]["title"] == f"Hail Hermes · {character_label(DEFAULTS['character'])}"

            update_settings(
                tmp_path,
                {"voice_session": "pinned", "pinned_session_id": "sess-2"},
            )
            await ws.send(json.dumps({"v": 2, "type": "text", "text": "add milk"}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                if msg.get("type") == "state" and msg.get("screen") == "idle":
                    break

            assert len(agent.calls) == 2
            assert agent.calls[1]["session_id"] == "sess-2"
            assert agent.calls[1]["new_session"] is False


def test_parakeet_worker_threads_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_POCKET_STATE", str(tmp_path))
    update_settings(tmp_path, {"stt_threads": 7})

    captured: dict[str, Any] = {}

    class _FakeOfflineRecognizer:
        @classmethod
        def from_transducer(cls, **kwargs: Any) -> "_FakeOfflineRecognizer":
            captured.update(kwargs)
            return cls()

    fake_sherpa = types.SimpleNamespace(OfflineRecognizer=_FakeOfflineRecognizer)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa)
    monkeypatch.setattr(parakeet_worker, "_protocol_stdout", lambda: open(os.devnull, "wb"))
    monkeypatch.setattr(parakeet_worker, "_serve", lambda out, rec: None)

    parakeet_worker.main(["--model-dir", str(tmp_path)])
    assert captured["num_threads"] == 7

    parakeet_worker.main(["--model-dir", str(tmp_path), "--threads", "3"])
    assert captured["num_threads"] == 3
