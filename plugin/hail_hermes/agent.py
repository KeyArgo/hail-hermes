"""Agent bridge backends.

An agent backend turns one user utterance from one device into a stream of reply text deltas:

    async for delta in backend.run(device, text): ...

Cancelling the consuming task must stop the underlying run (``interrupt`` relies on it).

``HermesCliAgent`` is the production backend. It drives Hermes through its documented
orchestrator entry point, ``hermes chat --query-file - --format stream-json`` (JSONL: ``system/init`` →
``text`` deltas / ``tool_use`` / ``tool_result`` → ``result``), resuming one Hermes session per
device so each phone/watch has its own continuous conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess

from .settings import read_settings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

log = logging.getLogger(__name__)

_KILL_GRACE_SECONDS = 1.5
_REJECTED_TOOLSETS: set[str] = set()


@dataclass
class DeviceInfo:
    device_id: str
    name: str = ""
    kind: str = "sim"
    caps: dict[str, bool] = field(default_factory=dict)


class AgentBackend(Protocol):
    def run(self, device: DeviceInfo, text: str) -> AsyncIterator[str]: ...


# Every wording Hermes has used when a session's stored model is no longer served. Matching
# only the first one let a poisoned session keep failing under a different message.
_MODEL_GONE = (
    "isn't available on",
    "not found. the requested model",
    "does not exist in our configuration",
    "no fallback model group found",
    # The one that actually broke the watch: a session created by another tool (a terminal
    # run) carries launch flags this hub does not send, and Hermes refuses to resume it.
    "resume retains gateway session policy",
    "differ from this session's launch",
)


def _agent_home() -> Path:
    """Where the child runs.

    Hermes injects AGENTS.md/SOUL.md from the working directory, so this is how the wrist
    gets its own voice instructions without touching the user's desktop sessions. Falls back
    to the home directory if the state directory cannot be written.
    """
    try:
        from .models import hermes_home

        path = hermes_home() / "hail-hermes" / "agent-home"
        path.mkdir(parents=True, exist_ok=True)
        guide = path / "AGENTS.md"
        # Rewrite when the wording changes, not only on first run: an install from yesterday would
        # otherwise keep the old voice instructions forever, and the old ones let the wrist agent
        # answer with a hand-off, narrate its tools and sign off like a written status report.
        try:
            current = guide.read_text(encoding="utf-8") if guide.exists() else ""
        except OSError:
            current = ""
        if current.strip() != _WATCH_VOICE.strip():
            guide.write_text(_WATCH_VOICE, encoding="utf-8")
        return path
    except Exception:
        return Path.home()


_WATCH_VOICE = """# Speaking through the watch

You are the user's own assistant, answering out loud through their wrist. These are not
instructions from a project you work on; they are how to sound when someone is listening.

## Answer, do not hand off

- Answer the question in the same turn. Never reply with only "On it", "Off I go", "Consider it done"
  or any other hand-off line — that reads as a broken assistant, because nothing follows it.
- Do not call device_say, device_notify or any speech tool to deliver a normal answer. Your reply is
  the answer; the hub speaks it for you. Those tools are for separate, deliberate pings.
- Do not narrate your tools, your process, or this file. Never say what you stored, delivered or
  pushed unless the user asked you to do exactly that.

## Sound like speech, not a report

- One or two sentences for a normal question. If the user asks for detail — an itinerary, a plan, a
  list of steps — give at most five short spoken points, not a document.
- No headings, bullets, tables, code blocks, file paths, version strings or identifiers.
- Never write status lines, emoji, checkmarks, or sections such as "AND THIS IS WHAT I NEED FROM YOU",
  "✅ I'M FREE" or "🛑 I NEED YOU". Those belong to written reports on the desktop, never to speech.
- Say numbers, times and names the way a person says them out loud.

## Know this person

- You already know them: use their memory, their past sessions and what they told you earlier in this
  conversation. Never ask them to repeat context you already have.
- If you cannot do something, say what you can do instead, in one sentence.
"""


def _model_is_gone(detail: str) -> bool:
    low = (detail or "").lower()
    return any(marker in low for marker in _MODEL_GONE)


class AgentError(RuntimeError):
    def __init__(self, message: str = "hermes run failed", detail: str = ""):
        super().__init__(message)
        self.detail = detail or message



class EchoAgent:
    """Dev/demo backend: replies ``"<prefix><text>"`` in word chunks."""

    def __init__(self, prefix: str = "You said: ", delay: float = 0.0):
        self.prefix = prefix
        self.delay = delay

    async def run(self, device: DeviceInfo, text: str) -> AsyncIterator[str]:
        words = f"{self.prefix}{text}".split(" ")
        for i, word in enumerate(words):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield word if i == len(words) - 1 else word + " "


class HermesCliAgent:
    """Runs each turn as ``hermes chat --query-file - --format stream-json [--resume SID]``.

    Session ids are captured from ``result`` events and handed to ``on_session`` so the hub can
    persist them per device. Cancellation terminates the process (SIGTERM, then SIGKILL)."""

    def __init__(
        self,
        binary: str | None = None,
        extra_args: list[str] | None = None,
        source: str | None = None,
        timeout: float = 600.0,
        session_lookup: Callable[[str], str | None] | None = None,
        on_session: Callable[[str, str], None] | None = None,
        env: dict[str, str] | None = None,
        toolsets: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        model_lookup: Callable[[], tuple[str, str]] | None = None,
    ):
        self.binary = binary or shutil.which("hermes") or "hermes"
        self.extra_args = [a for a in (extra_args or []) if a != "--yolo"]
        self.source = source
        self.timeout = timeout
        self.session_lookup = session_lookup or (lambda _d: None)
        self.on_session = on_session or (lambda _d, _s: None)
        self.env = env
        self.toolsets = toolsets
        self.model = model
        self.provider = provider
        # Read the live setting per turn so changing the model on the watch takes effect on the
        # next answer instead of demanding a hub restart.
        self.model_lookup = model_lookup
        self.last_session_id: str | None = None
        self.last_title: str | None = None

    def _live_model(self) -> tuple[str, str]:
        model, provider = self.model, self.provider
        if self.model_lookup is not None:
            try:
                live_model, live_provider = self.model_lookup()
                if live_provider:
                    # Trust the live pair completely: an empty model id means this provider's
                    # default, not "keep the startup model from somewhere else".
                    model, provider = live_model, live_provider
                elif live_model:
                    model = live_model
            except Exception:
                pass
        return model, provider

    def argv(
        self,
        device: DeviceInfo,
        text: str = "",
        *,
        session_id: str | None = None,
        new_session: bool = False,
        session_title: str | None = None,
    ) -> list[str]:
        del text
        if new_session:
            sid = None
        elif session_id is not None:
            sid = session_id or None
        else:
            sid = self.session_lookup(device.device_id)
        argv = [self.binary, "chat", "--query-file", "-", "--format", "stream-json"]
        if self.source:
            argv += ["--source", self.source]
        if self.toolsets and self.toolsets not in _REJECTED_TOOLSETS:
            argv += ["-t", self.toolsets]
        if sid:
            argv += ["--resume", sid]
        else:
            if session_title:
                argv += ["-c", session_title, "--create-if-missing"]
            model, provider = self._live_model()
            if model:
                argv += ["-m", model]
            if provider:
                argv += ["--provider", provider]
        return argv + [a for a in self.extra_args if a != "--yolo"]

    async def _terminate_child(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        if os.name == "nt":
            # terminate() would kill only a launcher or .cmd wrapper and orphan its child.
            await asyncio.to_thread(
                subprocess.run, ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False,
            )
            try:
                await asyncio.wait_for(proc.wait(), _KILL_GRACE_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                proc.kill()
                await proc.wait()
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), _KILL_GRACE_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()

    async def run(
        self,
        device: DeviceInfo,
        text: str,
        *,
        session_id: str | None = None,
        new_session: bool = False,
        title: str | None = None,
    ) -> AsyncIterator[str]:
        # A toolset Hermes doesn't know (e.g. "pocket" before the plugin is enabled) fails the whole run
        # with invalid_params before any output. Retry once with Hermes's default tools instead.
        self.last_title = title
        streamed = False
        try:
            async for delta in self._run_once(
                device, text, session_id=session_id, new_session=new_session, title=title
            ):
                streamed = True
                yield delta
        except AgentError as exc:
            # A session that was deleted, moved or never saved must not cost the user their
            # question: Hermes fails in about 40 ms with "No session found matching ...".
            # Start a fresh session and answer anyway.
            detail = getattr(exc, "detail", "") or ""
            if not streamed and "No session found" in detail:
                log.warning("session %s is gone; asking in a fresh session", session_id)
                if self.session_lookup is not None:
                    try:
                        self.on_session(device.device_id, "")
                    except Exception:
                        pass
                async for delta in self._run_once(
                    device, text, session_id=None, new_session=True, title=title
                ):
                    yield delta
                return
            if not streamed and session_id and _model_is_gone(detail):
                # A session born while the provider was misconfigured keeps asking for a model
                # that provider does not serve, and Hermes refuses model overrides on resume
                # ("Resume retains gateway session policy"). Start a fresh session so the
                # question still gets answered.
                log.warning("session %s asks for a model its provider no longer serves; "
                            "starting a fresh session", session_id)
                if self.on_session is not None:
                    try:
                        self.on_session(device.device_id, "")
                    except Exception:
                        pass
                async for delta in self._run_once(
                    device, text, session_id=None, new_session=True, title=title
                ):
                    yield delta
                return
            if streamed or not self.toolsets or "invalid_params" not in detail:
                raise
            log.warning("hermes rejected toolsets %r; retrying with Hermes's default tools", self.toolsets)
            _REJECTED_TOOLSETS.add(self.toolsets)
            async for delta in self._run_once(
                device, text, session_id=session_id, new_session=new_session, title=title
            ):
                yield delta

    async def _run_once(
        self,
        device: DeviceInfo,
        text: str,
        *,
        session_id: str | None = None,
        new_session: bool = False,
        title: str | None = None,
    ) -> AsyncIterator[str]:
        # Run from the user's home, never from a source checkout: Hermes auto-injects
        # AGENTS.md and SOUL.md from the working directory, so a repo checkout made the agent
        # answer like a developer reading project rules instead of the user's own assistant.
        workdir = os.environ.get("HAIL_AGENT_CWD") or str(_agent_home())
        env = {**os.environ, **(self.env or {}), "HERMES_POCKET_DEVICE_ID": device.device_id,
               "PYTHONIOENCODING": "utf-8"}
        cmd = self.argv(device, text, session_id=session_id, new_session=new_session,
                        session_title=title)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir,
        )

        async def _feed_stdin():
            try:
                proc.stdin.write(text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        stdin_task = asyncio.create_task(_feed_stdin()) if proc.stdin else None
        stderr_task = asyncio.create_task(proc.stderr.read())
        streamed = False
        result: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(self.timeout):
                async for raw in proc.stdout:
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    etype = event.get("type")
                    if etype == "result":
                        sid = event.get("session_id")
                        if sid:
                            self.last_session_id = sid
                        if sid and session_id is None:
                            self.on_session(device.device_id, sid)
                    if etype == "text" and event.get("text"):
                        streamed = True
                        yield event["text"]
                    elif etype == "result":
                        result = event
                await proc.wait()
                if stdin_task:
                    await stdin_task
        except BaseException:
            if stdin_task:
                stdin_task.cancel()
            stderr_task.cancel()
            await self._terminate_child(proc)
            raise
        err = (await stderr_task).decode(errors="replace")
        if result is None:
            detail_parts = [f"exit {proc.returncode}", "exited without a result"]
            if err.strip():
                detail_parts.append(err.strip()[-300:])
            detail = ": ".join(detail_parts)
            raise AgentError("hermes run failed", detail=detail)
        if result.get("exit_code") or result.get("error"):
            err_msg = result.get("error")
            code = result.get("exit_code")
            parts = []
            if code is not None and code != 0:
                parts.append(f"exit {code}")
            if err_msg:
                parts.append(str(err_msg))
            if err.strip():
                parts.append(err.strip()[-300:])
            detail = ": ".join(parts) if parts else "hermes run failed"
            raise AgentError("hermes run failed", detail=detail)
        if not streamed and result.get("text"):
            yield result["text"]


def build_agent(cfg: dict[str, Any] | None, store=None) -> AgentBackend:
    cfg = dict(cfg or {})
    backend = cfg.pop("backend", "hermes")
    if backend == "echo":
        return EchoAgent(cfg.get("prefix", "You said: "), float(cfg.get("delay", 0)))
    if backend == "hermes":
        return HermesCliAgent(
            binary=cfg.get("binary"),
            extra_args=cfg.get("extra_args"),
            source=cfg.get("source") or "cli",
            timeout=float(cfg.get("timeout", 600)),
            session_lookup=store.agent_session if store else None,
            on_session=store.set_agent_session if store else None,
            env=cfg.get("env"),
            toolsets=cfg.get("toolsets"),
            model=cfg.get("model"),
            provider=cfg.get("provider"),
            model_lookup=(
                lambda: (
                    str(read_settings(store.dir).get("model") or ""),
                    str(read_settings(store.dir).get("provider") or ""),
                )
            ) if store else None,
        )
    raise ValueError(f"unknown agent backend {backend!r} (hermes|echo)")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RULER_CHARS = set("-─═=_┼+│|╭╮╰╯├┤┬┴ ")


def parse_sessions_table(output: str) -> list[dict[str, str]]:
    """Parse ``hermes sessions list`` table output into ``[{id, title, age}]``."""
    clean = _ANSI_RE.sub("", output or "")
    lines = clean.splitlines()
    header_idx: int | None = None
    col_starts: tuple[int, int, int, int] | None = None
    pipe_mode = False

    for idx, line in enumerate(lines):
        low = line.lower()
        if "title" in low and "workspace" in low and "last active" in low and "id" in low:
            header_idx = idx
            if "|" in line or "│" in line:
                pipe_mode = True
            else:
                c_title = low.find("title")
                c_ws = low.find("workspace")
                c_age = low.find("last active")
                c_id = low.rfind("id")
                if 0 <= c_title < c_ws < c_age < c_id:
                    col_starts = (c_title, c_ws, c_age, c_id)
            break

    if header_idx is None:
        return []

    items: list[dict[str, str]] = []
    for line in lines[header_idx + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) <= _RULER_CHARS:
            continue
        low = stripped.lower()
        if stripped.startswith(("…", "...")) or "more not shown" in low:
            continue

        title = age = sid = folder = ""
        if pipe_mode:
            cells = [c.strip() for c in re.split(r"[|│]", stripped) if c.strip()]
            if len(cells) >= 4:
                title, folder, age, sid = cells[0], cells[1], cells[2], cells[3]
        else:
            parts = [p.strip() for p in re.split(r"\s{2,}", stripped) if p.strip()]
            if len(parts) >= 4:
                sid = parts[-1]
                age = parts[-2]
                folder = parts[-3]
                title = " ".join(parts[:-3]).strip()
            elif col_starts is not None and len(line) > col_starts[3]:
                c_title, c_ws, c_age, c_id = col_starts
                title = line[c_title:c_ws].strip()
                folder = line[c_ws:c_age].strip()
                age = line[c_age:c_id].strip()
                sid = line[c_id:].strip()

        if sid and age:
            items.append({"id": sid, "title": title or sid, "age": age, "folder": folder})
    return items


async def list_hermes_sessions(
    limit: int = 8,
    binary: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> list[dict[str, str]]:
    """Run ``hermes sessions list --limit N`` off the event loop with a 10 s timeout."""
    limit = max(1, min(30, int(limit)))
    exe = binary or shutil.which("hermes") or "hermes"
    proc_env = {**os.environ, **(env or {}), "PYTHONIOENCODING": "utf-8"}

    def _run() -> str:
        res = subprocess.run(
            [exe, "sessions", "list", "--limit", str(limit)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=proc_env,
            check=False,
        )
        if res.returncode != 0:
            return ""
        return res.stdout

    try:
        stdout = await asyncio.to_thread(_run)
    except Exception:
        return []
    return parse_sessions_table(stdout)[:limit]
