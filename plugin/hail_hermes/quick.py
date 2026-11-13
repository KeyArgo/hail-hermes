"""Quick ask agent backend: keyless local OpenAI-compatible streaming chat completions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import tempfile
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from .agent import AgentError, DeviceInfo
from . import filelock
from .facts import facts_path, read_facts
from .settings import read_settings
from .store import default_state_dir

HANDOFF_MARKER = "[[HERMES]]"
MAX_MEMORY_FACT_CHARS = 300
MAX_PROMPT_MEMORY_CHARS = 2000

SOUL_DEFAULT_PATH = Path(__file__).resolve().parent / "soul_default.md"
SYSTEM_PROMPT = (
    "You are a voice assistant on a watch. Reply in one or two short spoken sentences "
    "with plain text and no markdown. If the request needs tools, files, the user's data, "
    "or long work, reply with exactly [[HERMES]] and nothing else."
)


def _completions_url(url: str) -> str:
    base = url.rstrip("/")
    if base.endswith("/v1/chat/completions") or base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _models_url(url: str) -> str:
    base = url.rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1/models", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def fetch_endpoint_models(url: str, timeout: float = 3.0) -> list[str]:
    """Query an OpenAI-compatible base URL at ``/v1/models`` and return model IDs."""
    if not isinstance(url, str) or not url.strip():
        return []
    target = _models_url(url.strip())
    req = urllib.request.Request(target, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception:
        return []
    entries: Any = []
    if isinstance(payload, dict):
        entries = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("models", [])
    elif isinstance(payload, list):
        entries = payload
    models: list[str] = []
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("model") or item.get("name")
                if isinstance(mid, str) and mid.strip():
                    models.append(mid.strip())
            elif isinstance(item, str) and item.strip():
                models.append(item.strip())
    return models


def list_quick_models(endpoints: list[str], timeout: float = 3.0) -> list[dict[str, str]]:
    """Query each base URL in ``endpoints`` and return ``[{"url": url, "model": model}]``."""
    results: list[dict[str, str]] = []
    for raw_url in endpoints or []:
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        clean_url = raw_url.strip()
        for model_id in fetch_endpoint_models(clean_url, timeout=timeout):
            results.append({"url": clean_url, "model": model_id})
    return results


def ensure_soul_file(
    soul_path: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> Path:
    """Ensure ``soul.md`` exists at ``soul_path``, copying ``soul_default.md`` if missing."""
    if soul_path:
        target = Path(soul_path).expanduser()
    else:
        configured = read_settings(state_dir).get("soul_path")
        target = Path(str(configured)).expanduser()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if SOUL_DEFAULT_PATH.is_file():
            shutil.copyfile(SOUL_DEFAULT_PATH, target)
        else:
            target.write_text(
                "You are Hermes, a warm, upbeat, and genuinely helpful voice companion. "
                "Speak in plain words and short spoken sentences without markdown or lists.\n",
                encoding="utf-8",
            )
    return target


def _profile_memories_dir(state_dir: str | Path | None) -> Path:
    """Where core keeps ``USER.md`` for the active profile — read-only for this plugin.

    Resolved the way core resolves it (``HERMES_HOME`` may be a profile name, a profile directory or a
    ``.hermes`` root) and never written: only the facts file belongs to this plugin (review item 3).
    """
    base = Path("~/.hermes").expanduser()
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if not env_home:
        return base / "profiles" / "default" / "memories"
    if "/" not in env_home and "\\" not in env_home and not env_home.startswith("~"):
        return base / "profiles" / env_home / "memories"
    p = Path(env_home).expanduser()
    if p.parent.name == "profiles" or (p / "memories").exists():
        return p / "memories"
    if p.name == ".hermes" or (p / "profiles").exists():
        return p / "profiles" / "default" / "memories"
    return base / "profiles" / (p.name or "default") / "memories"


def _core_memory_path(state_dir: str | Path | None) -> Path:
    """Where Hermes keeps ``MEMORY.md`` — read only, never written by this plugin.

    Quality review: quick mode's prompt stopped including this file when the plugin stopped writing to it,
    so an upgrade silently emptied what quick answers knew about the owner. Reading it is not writing it:
    the agent still owns the format, the budget and the safety scan, and this plugin only ever looks.
    ``memory_path`` in Settings still points somewhere else when the owner has moved their profile.
    """
    if state_dir is not None:
        try:
            configured = str(read_settings(state_dir).get("memory_path") or "").strip()
        except Exception:
            configured = ""
        if configured:
            return Path(configured).expanduser()
    return _profile_memories_dir(state_dir) / "MEMORY.md"


def _close_response(resp: Any) -> None:
    with contextlib.suppress(Exception):
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        resp.close()


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _text(response: Any, kind: str) -> str:
    choices = _field(response, "choices") or []
    content = _field(_field(choices[0], kind), "content") if choices else None
    return content if isinstance(content, str) else ""


class HermesModelBackend:
    def stream(self, messages: list[dict[str, str]], stop: threading.Event) -> Iterator[str]:
        from agent.auxiliary_client import call_llm

        stream = None
        emitted = False
        try:
            stream = call_llm(messages=messages, stream=True)
            if _field(stream, "choices") is not None:
                chunks = (_text(stream, "message"),)
            else:
                chunks = (_text(chunk, "delta") for chunk in stream)
            for content in chunks:
                if stop.is_set():
                    return
                if content:
                    emitted = True
                    yield content
            if not emitted and not stop.is_set():
                raise RuntimeError("Hermes stream returned no text")
        except Exception:
            if emitted or stop.is_set():
                raise
            content = _text(call_llm(messages=messages, stream=False), "message")
            if not content:
                raise RuntimeError("Hermes returned no text")
            if not stop.is_set():
                yield content
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()


class QuickAgent:
    """Streams deltas from an OpenAI-compatible ``/v1/chat/completions`` endpoint using stdlib urllib."""

    def __init__(
        self,
        url: str,
        model: str,
        *,
        timeout: float = 30.0,
        system_prompt: str = SYSTEM_PROMPT,
        max_history: int = 12,
        soul_path: str | Path | None = None,
        state_dir: str | Path | None = None,
    ):
        self.url = _completions_url(url) if url.strip() else ""
        self.backend = None if self.url else HermesModelBackend()
        self.model = model
        self.timeout = timeout
        self._rules_prompt = system_prompt
        self._custom_system_prompt: str | None = system_prompt if system_prompt != SYSTEM_PROMPT else None
        self.state_dir: Path | None = Path(state_dir).expanduser() if state_dir else None
        self.soul_path: Path | None = ensure_soul_file(soul_path, state_dir=state_dir)
        self._soul_mtime: float | None = None
        self._soul_text: str = ""
        self._memory_file: Path | None = None
        self._memory_mtime: float | None = None
        self._memory_text: str = ""
        self._user_file: Path | None = None
        self._user_mtime: float | None = None
        self._user_text: str = ""
        self._core_file: Path | None = None
        self._core_mtime: float | None = None
        self._core_text: str = ""
        self._refresh_soul()
        self._refresh_memories()
        self.max_history = max_history
        self._history: dict[str, list[dict[str, str]]] = {}
        self._history_times: dict[str, float] = {}
        self._history_loaded = False
        self._history_path = (self.state_dir or default_state_dir()) / "quick_history.json"

    def _refresh_soul(self) -> None:
        if self.soul_path is None:
            return
        try:
            if not self.soul_path.exists():
                ensure_soul_file(self.soul_path)
            stat = self.soul_path.stat()
            mtime = stat.st_mtime
            if self._soul_mtime != mtime:
                self._soul_text = self.soul_path.read_text(encoding="utf-8").strip()
                self._soul_mtime = mtime
        except OSError:
            pass

    def _effective_state_dir(self) -> Path | None:
        if self.state_dir is not None:
            return self.state_dir
        if self.soul_path is not None and (self.soul_path.parent / "settings.json").exists():
            return self.soul_path.parent
        return None

    def _refresh_memories(self) -> None:
        # Facts this plugin was asked to remember live in its own file (review item 3): core's
        # MEMORY.md has an entry format, a budget and a safety scan that a direct append bypasses.
        state = self._effective_state_dir()
        user_path = _profile_memories_dir(state) / "USER.md"
        mem_path = facts_path(state) if state is not None else Path("")
        core_path = _core_memory_path(state)
        if self._memory_file != mem_path:
            self._memory_file = mem_path
            self._memory_mtime = None
            self._memory_text = ""
        if self._user_file != user_path:
            self._user_file = user_path
            self._user_mtime = None
            self._user_text = ""
        if self._core_file != core_path:
            self._core_file = core_path
            self._core_mtime = None
            self._core_text = ""
        try:
            stat = mem_path.stat()
            if self._memory_mtime != stat.st_mtime:
                # Through the facts reader, so a prompt never picks up a half-written fact: it takes the
                # same lock an append or a trim does.
                raw = "\n".join(f"- {fact}" for fact in read_facts(state)) if state is not None else ""
                self._memory_text = raw[-MAX_PROMPT_MEMORY_CHARS:] if raw else ""
                self._memory_mtime = stat.st_mtime
        except OSError:
            self._memory_mtime = None
            self._memory_text = ""
        try:
            # Read only: the same bytes the agent's memory tool keeps, so nothing an owner already told
            # Hermes disappears from quick answers after an upgrade.
            stat = core_path.stat()
            if self._core_mtime != stat.st_mtime:
                raw = core_path.read_text(encoding="utf-8").strip()
                self._core_text = raw[-MAX_PROMPT_MEMORY_CHARS:] if raw else ""
                self._core_mtime = stat.st_mtime
        except OSError:
            self._core_mtime = None
            self._core_text = ""
        try:
            stat = user_path.stat()
            if self._user_mtime != stat.st_mtime:
                raw = user_path.read_text(encoding="utf-8").strip()
                self._user_text = raw[-MAX_PROMPT_MEMORY_CHARS:] if raw else ""
                self._user_mtime = stat.st_mtime
        except OSError:
            self._user_mtime = None
            self._user_text = ""

    @property
    def system_prompt(self) -> str:
        self._refresh_soul()
        self._refresh_memories()
        base_rules = self._custom_system_prompt or self._rules_prompt
        parts: list[str] = []
        if self._soul_text:
            parts.append(self._soul_text)
        parts.append(base_rules)
        if self._core_text:
            parts.append(f"Memory from Hermes (read-only; the agent owns it):\n{self._core_text}")
        if self._memory_text:
            parts.append(f"Facts you were asked to remember:\n{self._memory_text}")
        if self._user_text:
            parts.append(f"User (USER.md):\n{self._user_text}")
        return "\n\n".join(parts)

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self._custom_system_prompt = value

    def _load_history(self) -> None:
        if self._history_loaded:
            return
        self._history_loaded = True
        try:
            data = json.loads(self._history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for device_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            timestamp, messages = entry.get("last_turn"), entry.get("messages")
            if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                    or not isinstance(messages, list) or len(messages) % 2):
                continue
            if any(not isinstance(m, dict) or m.get("role") != ("user" if i % 2 == 0 else "assistant")
                   or not isinstance(m.get("content"), str) for i, m in enumerate(messages)):
                continue
            limit = max(0, self.max_history) * 2
            self._history[device_id] = messages[-limit:] if limit else []
            self._history_times[device_id] = timestamp

    def _save_history(self) -> None:
        temp_name = None
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._history_path.parent,
                                             prefix=".quick_history.", delete=False) as fh:
                temp_name = fh.name
                json.dump({device_id: {"last_turn": self._history_times[device_id], "messages": messages}
                           for device_id, messages in self._history.items()}, fh)
            os.replace(temp_name, self._history_path)
        except OSError:
            pass
        finally:
            if temp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)

    def clear(self, device_id: str) -> None:
        """Clear the device's conversation history, including its persisted copy."""
        self._load_history()
        self._history.pop(device_id, None)
        self._history_times.pop(device_id, None)
        self._save_history()

    def history(self, device_id: str) -> list[dict[str, str]]:
        self._load_history()
        if device_id in self._history_times and time.time() - self._history_times[device_id] >= 1800:
            self.clear(device_id)
        return [dict(message) for message in self._history.get(device_id, ())]

    def record_turn(self, device_id: str, text: str, reply: str) -> None:
        prior = self.history(device_id)
        limit = max(0, self.max_history) * 2
        self._history[device_id] = (prior + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ])[-limit:] if limit else []
        self._history_times[device_id] = time.time()
        self._save_history()

    async def run(self, device: DeviceInfo, text: str) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | BaseException | None] = asyncio.Queue()
        stop_event = threading.Event()
        lock = threading.Lock()
        resp_holder: list[Any] = []

        prior = self.history(device.device_id)
        messages = [
            {"role": "system", "content": self.system_prompt},
            *prior,
            {"role": "user", "content": text},
        ]
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "max_tokens": 200,
                "temperature": 0.3,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode("utf-8")

        def _put(item: str | BaseException | None) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, item)

        def _worker() -> None:
            resp = None
            try:
                if self.backend is not None:
                    for content in self.backend.stream(messages, stop_event):
                        _put(content)
                    return
                req = urllib.request.Request(
                    self.url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    method="POST",
                )
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                with lock:
                    if stop_event.is_set():
                        _close_response(resp)
                        return
                    resp_holder.append(resp)

                for raw_line in resp:
                    if stop_event.is_set():
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                    except ValueError:
                        continue
                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice.get("delta")
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if content is None and isinstance(choice.get("message"), dict):
                        content = choice["message"].get("content")
                    if isinstance(content, str) and content:
                        _put(content)
            except BaseException as exc:
                if not stop_event.is_set():
                    _put(AgentError("quick agent failed", detail=str(exc)))
            finally:
                if resp is not None:
                    _close_response(resp)
                _put(None)

        thread = threading.Thread(target=_worker, name="hail-hermes-quick", daemon=True)
        thread.start()

        parts: list[str] = []
        completed = False
        try:
            while True:
                item = await queue.get()
                if item is None:
                    completed = True
                    break
                if isinstance(item, BaseException):
                    raise item
                parts.append(item)
                yield item
        finally:
            stop_event.set()
            with lock:
                for resp in resp_holder:
                    _close_response(resp)

        if completed:
            reply = "".join(parts).strip()
            if reply and not reply.startswith(HANDOFF_MARKER):
                self.record_turn(device.device_id, text, reply)
