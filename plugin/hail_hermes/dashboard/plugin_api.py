"""Hail Hermes dashboard API for the Desktop Wrist pane.

Mounted by Hermes at ``/api/plugins/hail-hermes/``. Proxies pairing and hub
status through the loopback control channel.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import secrets
import signal
import struct
import threading
import time
import wave
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

# Hermes loads this file from a directory called ``hail-hermes``, which is not a valid
# package name, so ``import hail_hermes`` failed and every route in this module 404'd —
# the pane's Approve button said "404: detail not found" while the code looked fine.
try:  # pragma: no cover - import shim
    import hail_hermes  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    import importlib.util
    import sys

    _plugin_dir = Path(__file__).resolve().parents[1]
    _spec = importlib.util.spec_from_file_location(
        "hail_hermes", _plugin_dir / "__init__.py", submodule_search_locations=[str(_plugin_dir)]
    )
    if _spec and _spec.loader:
        _module = importlib.util.module_from_spec(_spec)
        sys.modules["hail_hermes"] = _module
        _spec.loader.exec_module(_module)

from hail_hermes.agent import list_hermes_sessions
from hail_hermes.autostart import HubStartError, ensure_hub
from hail_hermes import filelock
from hail_hermes.control import ControlClient, HubUnavailable
from hail_hermes.quick import fetch_endpoint_models, list_quick_models
from hail_hermes.kokoro_worker import parse_voice_spec
from hail_hermes.settings import (
    CHARACTERS,
    CHOICES,
    read_settings,
    update_settings,
)
from hail_hermes.setup_check import run_setup_check
from hail_hermes.store import Store, atomic_write
from ..facts import append_fact, facts_path, read_facts

router = APIRouter()

_HUB_HINT = "Hail Hermes could not start."
_SAMPLE_PATH = Path(__file__).resolve().parent.parent / "assets" / "sample-question.wav"
_PROC_ROOT = Path("/proc")
_LAST_MODELS: list[dict[str, str]] = []
_CONTROL_TIMEOUT = 1.0
_ROUTE_BUDGET = 0.25
# Model discovery does real network I/O (and Windows connect-refused alone takes ~2 s),
# so it needs a larger budget than the loopback control calls.
_MODELS_BUDGET = 6.0
_AUTOSTART_LOCK = threading.Lock()
_AUTOSTART_THREAD: threading.Thread | None = None
_AUTOSTART_CACHE: dict[str, Any] = {"started": False, "error": None}
KOKORO_VOICES: list[str] = [
    "af_heart",
    "af_bella",
    "am_michael",
    "am_adam",
    "am_puck",
    "bm_george",
    "bm_fable",
]
DEFAULT_SOUL_TEXT = (
    "You are Hermes on the user's wrist: concise, warm, direct, and spoken-friendly.\n"
    "Keep answers brief and natural for voice playback unless asked for detail.\n"
)
WATCH_CONNECT_STEPS: list[str] = [
    "1. Start the Hail Hermes (`hermes hail watch` or click Start hub above).",
    "2. Put the watch and this computer on the same Wi-Fi / LAN (or USB ADB reverse).",
    "3. Open Hail Hermes on your watch and tap Pair / Connect.",
    "4. Verify the 4-digit match code shown here and click Approve.",
]


class _ModelList(list):
    """List of ``{url, model}`` dicts that also supports dict-style ``['models']`` lookup."""

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("models", "items", "choices"):
            return list(self)
        return default

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str) and key in ("models", "items", "choices"):
            return list(self)
        return super().__getitem__(key)

    def __contains__(self, item: Any) -> bool:
        if isinstance(item, str) and item in ("models", "items", "choices"):
            return True
        return super().__contains__(item)


def _resolve_state_dir() -> str | None:
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        for path in (
            ("plugins", "entries", "hail-hermes", "settings", "state_dir"),
            ("plugins", "entries", "hail-hermes", "config", "state_dir"),
            ("plugins", "entries", "hail-hermes", "state_dir"),
            ("plugins", "entries", "hermes-pocket", "settings", "state_dir"),
            ("plugins", "entries", "hermes-pocket", "config", "state_dir"),
            ("plugins", "entries", "hermes-pocket", "state_dir"),
        ):
            raw = cfg_get(cfg, *path)
            if raw:
                return str(raw)
    except Exception:
        pass
    return None


def _client(state_dir: str | Path | None = None) -> ControlClient:
    resolved = state_dir if state_dir is not None else _resolve_state_dir()
    try:
        if resolved:
            return ControlClient(resolved, timeout=_CONTROL_TIMEOUT)
        return ControlClient(timeout=_CONTROL_TIMEOUT)
    except TypeError:
        if resolved:
            return ControlClient(resolved)
        return ControlClient()


def _run_bounded(fn: Any, *args: Any, timeout: float = _ROUTE_BUDGET, **kwargs: Any) -> Any:
    result: list[Any] = []
    error: list[BaseException] = []

    def _target() -> None:
        try:
            result.append(fn(*args, **kwargs))
        except BaseException as exc:
            error.append(exc)

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(max(0.01, timeout))
    if worker.is_alive():
        raise HubUnavailable("control call timed out")
    if error:
        raise error[0]
    return result[0] if result else None


def _control_call(client: ControlClient, op: str, **kwargs: Any) -> Any:
    return _run_bounded(client.call, op, timeout=_ROUTE_BUDGET, **kwargs)


def _run_ensure_hub(state_dir: str | Path | None) -> None:
    try:
        started = bool(ensure_hub(state_dir))
        with _AUTOSTART_LOCK:
            _AUTOSTART_CACHE["started"] = started
            _AUTOSTART_CACHE["error"] = None
    except HubStartError as exc:
        with _AUTOSTART_LOCK:
            _AUTOSTART_CACHE["started"] = False
            _AUTOSTART_CACHE["error"] = str(exc) or _HUB_HINT
    except Exception as exc:
        with _AUTOSTART_LOCK:
            _AUTOSTART_CACHE["started"] = False
            _AUTOSTART_CACHE["error"] = str(exc) or _HUB_HINT


def _kick_ensure_hub(state_dir: str | Path | None, *, wait: float = 0.0) -> bool:
    global _AUTOSTART_THREAD
    spawned: threading.Thread | None = None
    with _AUTOSTART_LOCK:
        if _AUTOSTART_THREAD is not None and _AUTOSTART_THREAD.is_alive():
            return True
        spawned = threading.Thread(
            target=_run_ensure_hub,
            args=(state_dir,),
            name="hail-hermes-ensure-hub",
            daemon=True,
        )
        _AUTOSTART_THREAD = spawned
        spawned.start()
    if wait > 0 and spawned is not None:
        spawned.join(timeout=wait)
    return spawned.is_alive()


def _pane_device_id(state_dir: str | Path | None) -> str:
    """Return the stable identity shared by all panes of this installation."""
    directory = Store(state_dir).dir
    path = directory / "pane.json"
    with (directory / "pane.lock").open("a+") as lock:
        filelock.lock(lock)
        try:
            try:
                data = json.loads(path.read_text())
                device_id = data.get("device_id") if isinstance(data, dict) else None
                if isinstance(device_id, str) and device_id:
                    return device_id
            except (OSError, ValueError):
                pass
            device_id = secrets.token_hex(16)
            atomic_write(path, json.dumps({"device_id": device_id}) + "\n")
            return device_id
        finally:
            filelock.unlock(lock)


def _hub_down(hint: str | None = None) -> dict[str, Any]:
    return {"running": False, "hint": hint or _HUB_HINT}


def _open_client(state_dir: str | Path | None = None) -> ControlClient:
    if state_dir is not None:
        try:
            return _client(state_dir)
        except TypeError:
            return _client()
    return _client()


def _try_client(state_dir: str | Path | None = None) -> tuple[ControlClient | None, dict[str, Any] | None]:
    try:
        return _open_client(state_dir), None
    except Exception as exc:
        return None, _hub_down(str(exc) or None)


def _read_live(client: ControlClient) -> dict[str, Any] | None:
    try:
        data = _run_bounded(client.live, timeout=_ROUTE_BUDGET)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_connected(client: ControlClient) -> list[dict[str, Any]]:
    try:
        rows = _run_bounded(client.connected, timeout=_ROUTE_BUDGET)
        return list(rows) if isinstance(rows, list) else []
    except Exception:
        return []


def _resolve_request_id(body: dict[str, Any] | None) -> str | None:
    if not body:
        return None
    rid = body.get("request_id")
    if rid:
        return str(rid)
    mc = body.get("match_code")
    if mc is not None:
        return str(mc)
    return None


def _loopback(remote: Any) -> bool:
    if not isinstance(remote, str):
        return False
    try:
        address = ipaddress.ip_address(remote.split("%", 1)[0])
        mapped = getattr(address, "ipv4_mapped", None)
        return (mapped or address).is_loopback
    except ValueError:
        return False


@router.get("/v1/version")
def version_route() -> dict[str, Any]:
    """Which build is answering: version, commit, dates, dirty state, channel.

    The badge in the app reads this route rather than carrying a number, so the two cannot
    disagree. ``version_stamp`` resolves BUILD_INFO.json -> live git -> plugin.yaml.
    """
    try:
        from ..version_stamp import as_dict
    except Exception:  # pragma: no cover - the stamp is optional, the answer still is not
        return {"version": "unknown", "commit": "", "commit_short": "", "commit_date": "",
                "built_at": "", "channel": "dev", "dirty": None, "source": "manifest"}
    return as_dict()


@router.get("/identity")
def identity() -> dict[str, str]:
    """Return the owner pane's persistent pairing identity."""
    return {"device_id": _pane_device_id(_resolve_state_dir())}


@router.get("/status")
def status() -> dict[str, Any]:
    try:
        state_dir = _resolve_state_dir()
        device_id = _pane_device_id(state_dir)
        client, down = _try_client(state_dir)
        if down is not None:
            return down
        assert client is not None
        live_path = Store(state_dir).dir / "live.json"
        live = _read_live(client)
        if not live or not live_path.is_file():
            _kick_ensure_hub(state_dir, wait=0.05)
            live = _read_live(client)
        with _AUTOSTART_LOCK:
            started = bool(_AUTOSTART_CACHE.get("started"))
            cached_error = _AUTOSTART_CACHE.get("error")
        if not live:
            return _hub_down(str(cached_error) if cached_error else None)
        port = int(live.get("port") or 8767)
        error: str | None = str(cached_error) if cached_error else None
        raw_devices = live.get("devices")
        if isinstance(raw_devices, list):
            devices = list(raw_devices)
        else:
            try:
                rows = _control_call(client, "devices")
                devices = list(rows) if isinstance(rows, list) else _read_connected(client)
            except (HubUnavailable, OSError):
                return _hub_down()
            except RuntimeError as exc:
                error = str(exc)
                devices = _read_connected(client)
        out: dict[str, Any] = {
            "running": True,
            "started": started,
            "device_id": device_id,
            "port": port,
            "ws_url": f"ws://127.0.0.1:{port}/pocket",
            "devices": devices,
            "mirror": live.get("mirror"),
        }
        if error:
            out["error"] = error
        return out
    except Exception as exc:
        return _hub_down(str(exc) or None)


@router.post("/start")
def start_hub() -> dict[str, Any]:
    try:
        state_dir = _resolve_state_dir()
        starting = _kick_ensure_hub(state_dir, wait=0.0)
        client, down = _try_client(state_dir)
        if down is not None:
            return down
        assert client is not None
        live = _read_live(client)
        with _AUTOSTART_LOCK:
            started = bool(_AUTOSTART_CACHE.get("started"))
            cached_error = _AUTOSTART_CACHE.get("error")
        if live:
            return {"ok": True, "running": True, "started": started, "starting": starting}
        return {
            "ok": True,
            "running": False,
            "started": started,
            "starting": starting,
            "hint": str(cached_error) if cached_error else _HUB_HINT,
        }
    except Exception as exc:
        return _hub_down(str(exc) or None)


@router.get("/pending")
def pending() -> dict[str, Any]:
    client, down = _try_client()
    if down is not None:
        return {**down, "pending": []}
    assert client is not None
    if not _read_live(client):
        return {**_hub_down(), "pending": []}
    try:
        rows = _control_call(client, "pair_list")
    except HubUnavailable:
        return {**_hub_down(), "pending": []}
    except RuntimeError as exc:
        return {"running": True, "error": str(exc), "pending": []}
    except Exception as exc:
        return {**_hub_down(str(exc) or None), "pending": []}
    return {"running": True, "pending": list(rows or [])}


@router.post("/approve-self")
def approve_self(body: dict[str, Any] | None = None) -> dict[str, Any]:
    # These dashboard routes are reachable only through the authenticated
    # Hermes Desktop session. The request ID alone grants no authority.
    request_id = body.get("request_id") if isinstance(body, dict) else None
    if not isinstance(request_id, str) or not request_id:
        return {"ok": False}
    try:
        state_dir = _resolve_state_dir()
        device_id = _pane_device_id(state_dir)
        client = _open_client(state_dir)
        if not _read_live(client):
            return {"ok": False}
        rows = _control_call(client, "pair_list")
        matches = [
            row for row in rows or []
            if isinstance(row, dict) and row.get("request_id") == request_id
        ]
        if len(matches) != 1:
            return {"ok": False}
        row = matches[0]
        if (
            row.get("device_id") != device_id
            or row.get("kind") != "desktop"
            or not _loopback(row.get("remote"))
        ):
            return {"ok": False}
        # Request IDs are unique to a connection and cannot be reused. The hub
        # rechecks expiry, connection state, and single use when deciding.
        _control_call(client, "pair_approve", request_id=request_id)
        return {"ok": True}
    except HubUnavailable as exc:
        return {"ok": False, **_hub_down(str(exc) or None)}
    except RuntimeError:
        return {"ok": False}
    except Exception as exc:
        return {"ok": False, **_hub_down(str(exc) or None)}


@router.post("/approve")
def approve(body: dict[str, Any] | None = None) -> dict[str, Any]:
    request_id = _resolve_request_id(body)
    if not request_id:
        return {"error": "request_id required"}
    client, down = _try_client()
    if down is not None:
        return down
    assert client is not None
    if not _read_live(client):
        return _hub_down()
    try:
        result = _control_call(client, "pair_approve", **({"match_code": request_id} if body and body.get("match_code") is not None else {"request_id": request_id}))
    except HubUnavailable:
        return _hub_down()
    except (LookupError, RuntimeError) as exc:
        # The hub explains why a code is dead in a sentence a person can act on ("That code has
        # expired. Ask the watch to show a new one."). LookupError used to fall through to the
        # generic branch below, which threw the sentence away and told the owner nothing useful.
        return {"error": str(exc)}
    except Exception as exc:
        return _hub_down(str(exc) or None)
    return {"ok": True, "result": result}


@router.post("/deny")
def deny(body: dict[str, Any] | None = None) -> dict[str, Any]:
    request_id = _resolve_request_id(body)
    if not request_id:
        return {"error": "request_id required"}
    client, down = _try_client()
    if down is not None:
        return down
    assert client is not None
    if not _read_live(client):
        return _hub_down()
    try:
        _control_call(client, "pair_deny", **({"match_code": request_id} if body and body.get("match_code") is not None else {"request_id": request_id}))
    except HubUnavailable:
        return _hub_down()
    except RuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return _hub_down(str(exc) or None)
    return {"ok": True}


@router.get("/sample")
def sample() -> dict[str, Any]:
    """JSON wrapper for the bundled WAV."""
    if not _SAMPLE_PATH.is_file():
        return {"error": "sample question audio is missing"}
    data = _SAMPLE_PATH.read_bytes()
    return {"base64": base64.b64encode(data).decode("ascii")}


@router.get("/sample.wav")
def sample_wav() -> FileResponse:
    return FileResponse(
        _SAMPLE_PATH,
        media_type="audio/wav",
        filename="sample-question.wav",
    )


def _live_pid(state_dir: str | Path | None) -> int | None:
    live_path = Store(state_dir).dir / "live.json"
    try:
        data = json.loads(live_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        try:
            data = _read_live(_open_client(state_dir))
        except Exception:
            data = None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        return pid
    return None


def _pid_cmdline(pid: int) -> str:
    try:
        raw = (_PROC_ROOT / str(pid) / "cmdline").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return raw.replace("\x00", " ")


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def _pair_is_allowed(quick_url: str, quick_model: str) -> bool:
    norm_url = _normalize_url(quick_url)
    for item in _LAST_MODELS:
        if _normalize_url(item.get("url", "")) == norm_url and item.get("model") == quick_model:
            return True
    try:
        reachable = _run_bounded(fetch_endpoint_models, quick_url, timeout=_ROUTE_BUDGET)
    except Exception:
        reachable = []
    if quick_model in reachable:
        _LAST_MODELS.append({"url": quick_url, "model": quick_model})
        return True
    return False


def _notify_hub_settings_changed(state_dir: str | Path | None) -> None:
    try:
        client = _open_client(state_dir)
        if _read_live(client):
            _control_call(client, "reload_settings")
    except Exception:
        # A hub that is intentionally off is normal; debug keeps it visible without flooding.
        log.debug("hub settings notify failed", exc_info=True)


def _list_sessions_fast(state_dir: str | Path | None) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    ordered: list[dict[str, str]] = []
    try:
        client = _open_client(state_dir)
        if _read_live(client):
            rows = _control_call(client, "sessions", limit=30)
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and isinstance(row.get("id"), str):
                        sid = row["id"].strip()
                        if sid and sid not in seen:
                            title = str(row.get("title") or "")
                            seen[sid] = title
                            ordered.append({"id": sid, "title": title})
    except Exception:
        pass
    try:
        def _fetch_sessions() -> Any:
            return asyncio.run(list_hermes_sessions(limit=30))

        rows = _run_bounded(_fetch_sessions, timeout=0.15)
        for row in rows or []:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                sid = row["id"].strip()
                if sid and sid not in seen:
                    title = str(row.get("title") or "")
                    seen[sid] = title
                    ordered.append({"id": sid, "title": title})
    except Exception:
        pass
    return ordered


def _lookup_session_titles(state_dir: str | Path | None) -> dict[str, str]:
    return {row["id"]: row.get("title", "") for row in _list_sessions_fast(state_dir)}


def _default_soul_text() -> str:
    try:
        from hail_hermes import soul as soul_mod  # type: ignore[attr-defined]

        for attr in ("DEFAULT_SOUL", "DEFAULT_SOUL_MD", "default_soul"):
            val = getattr(soul_mod, attr, None)
            if callable(val):
                val = val()
            if isinstance(val, str) and val.strip():
                return val
    except Exception:
        pass
    return DEFAULT_SOUL_TEXT


def _resolve_soul_path(state_dir: str | Path | None) -> Path:
    cfg = read_settings(state_dir)
    raw = str(cfg.get("soul_path") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Store(state_dir).dir / "soul.md"


def _memory_file(state_dir: str | Path | None) -> Path:
    """The plugin's own facts file (review item 3) — never core's ``MEMORY.md``."""
    if state_dir is None:
        state_dir = _resolve_state_dir()
    return facts_path(state_dir)


def _read_memory_lines(state_dir: str | Path | None) -> tuple[list[str], Path]:
    path = _memory_file(state_dir)
    facts = read_facts(path.parent)
    return [f"- {fact}" for fact in facts], path

def _fallback_wav_bytes(duration_s: float = 0.15, rate: int = 16000) -> bytes:
    samples = max(1, int(duration_s * rate))
    buf = BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{samples}h", *([0] * samples)))
    return buf.getvalue()


@router.get("/models")
def get_models() -> list[dict[str, str]]:
    global _LAST_MODELS
    values = read_settings(_resolve_state_dir())
    endpoints = values.get("quick_endpoints")
    if not isinstance(endpoints, list):
        endpoints = []
    try:
        models = _run_bounded(list_quick_models, endpoints, timeout=_MODELS_BUDGET)
    except Exception:
        models = []
    _LAST_MODELS = list(models)
    return _ModelList(models)


@router.get("/hermes-models")
def hermes_models() -> dict[str, Any]:
    """Models the owner already has configured in Hermes, grouped by provider.

    The pane offers these so a spoken turn can be pointed at any model the user
    actually runs — not at a hardcoded default they cannot change from the UI.
    """
    provider = ""
    model = ""
    providers: dict[str, list[str]] = {}
    # One source of truth: whatever the watch may switch to, the pane offers. This includes
    # models probed live from an authorised endpoint, not just whatever was cached.
    try:
        from hail_hermes import models as models_api

        for row in models_api.available_models():
            providers.setdefault(str(row["source"]), [])
            if row["id"] not in providers[str(row["source"])]:
                providers[str(row["source"])].append(row["id"])
    except Exception:
        pass
    try:
        home = _hermes_home()
        cfg_path = home / "config.yaml"
        if cfg_path.exists():
            import yaml  # local import: only this route needs it

            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            model_cfg = cfg.get("model") or {}
            if isinstance(model_cfg, dict):
                provider = str(model_cfg.get("provider") or "")
                model = str(model_cfg.get("default") or model_cfg.get("model") or "")
            for name, entry in (cfg.get("providers") or {}).items():
                if not isinstance(entry, dict):
                    continue
                default_model = entry.get("default_model")
                if default_model:
                    providers.setdefault(str(name), []).append(str(default_model))
        cache_path = home / "provider_models_cache.json"
        if cache_path.exists():
            import json as _json

            cached = _json.loads(cache_path.read_text(encoding="utf-8")) or {}
            for name, entry in cached.items():
                models = entry.get("models") if isinstance(entry, dict) else entry
                if not isinstance(models, list):
                    continue
                bucket = providers.setdefault(str(name), [])
                for item in models:
                    mid = item.get("id") if isinstance(item, dict) else item
                    if isinstance(mid, str) and mid and mid not in bucket:
                        bucket.append(mid)
    except Exception:
        # A broken config must not take the pane down; an empty list is honest.
        pass
    if provider and model:
        providers.setdefault(provider, [])
        if model not in providers[provider]:
            providers[provider].insert(0, model)
    return {"provider": provider, "model": model, "providers": providers}


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    candidates: list[Path] = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            candidates.append(Path(local) / "hermes")
    candidates.append(Path.home() / ".hermes")
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[-1]


@router.get("/setup-check")
def setup_check() -> dict[str, Any]:
    return run_setup_check()


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    values = read_settings(_resolve_state_dir())
    return {
        **values,
        "values": dict(values),
        "settings": dict(values),
        "choices": {k: list(v) for k, v in CHOICES.items()},
        "kokoro_voices": list(KOKORO_VOICES),
        "characters": CHARACTERS,
        "watch_steps": list(WATCH_CONNECT_STEPS),
    }


# Saves read the current settings and then write; one at a time so two saves cannot drop each other's changes.
_settings_save_lock = threading.Lock()


@router.post("/settings")
def post_settings(body: dict[str, Any] | None = None) -> dict[str, Any]:
    with _settings_save_lock:
        return _save_settings(body)


def _save_settings(body: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "settings must be an object"}
    state_dir = _resolve_state_dir()
    payload = dict(body)
    add_endpoint = payload.pop("add_endpoint", None)
    if add_endpoint is not None:
        if not isinstance(add_endpoint, str) or not add_endpoint.strip():
            return {"ok": False, "error": "add_endpoint must be a non-empty URL string"}
        current_eps = list(read_settings(state_dir).get("quick_endpoints") or [])
        norm_ep = _normalize_url(add_endpoint)
        if norm_ep not in [_normalize_url(u) for u in current_eps]:
            current_eps.append(add_endpoint.strip())
        payload["quick_endpoints"] = current_eps
    if "quick_url" in payload or "quick_model" in payload:
        current = read_settings(state_dir)
        quick_url = payload.get("quick_url", current.get("quick_url", ""))
        quick_model = payload.get("quick_model", current.get("quick_model", ""))
        if not isinstance(quick_url, str) or not isinstance(quick_model, str):
            return {"ok": False, "error": "quick_url and quick_model must be strings"}
        quick_url = quick_url.strip()
        quick_model = quick_model.strip()
        if quick_url or quick_model:
            if not quick_url or not quick_model:
                return {"ok": False, "error": "both quick_url and quick_model are required"}
            if not _pair_is_allowed(quick_url, quick_model):
                return {
                    "ok": False,
                    "error": "quick_url and quick_model must be in the last model listing or reachable now",
                }
    previous = read_settings(state_dir)
    try:
        updated = update_settings(state_dir, payload)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    _notify_hub_settings_changed(state_dir)
    return {
        "ok": True,
        "saved": True,
        "restart_needed": any(previous[key] != updated[key] for key in (
            "model", "provider", "toolsets", "stt", "tts", "quick_url", "quick_model", "soul_path"
        )),
        "settings": updated,
        "values": updated,
    }


@router.post("/slots")
def post_slots(body: dict[str, Any] | list[Any] | None = None) -> dict[str, Any]:
    raw_items: Any = None
    if isinstance(body, list):
        raw_items = body
    elif isinstance(body, dict):
        for key in ("slots", "session_ids", "ids", "items"):
            if key in body:
                raw_items = body[key]
                break
    if not isinstance(raw_items, list) or len(raw_items) > 3:
        return {"ok": False, "error": "slots must be a list of up to 3 session ids"}
    state_dir = _resolve_state_dir()
    titles = _lookup_session_titles(state_dir)
    slots: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_items):
        if isinstance(entry, str):
            sid = entry.strip()
            if sid == "new":
                sid = ""
            name = ""
            fallback_title = ""
            slot_num = None
        elif isinstance(entry, dict):
            raw_sid = entry.get("id", "")
            raw_name = entry.get("name", "")
            raw_title = entry.get("title", "")
            if not isinstance(raw_sid, str) or not isinstance(raw_name, str):
                return {"ok": False, "error": "each slot must have string id and name"}
            sid = raw_sid.strip()
            if sid == "new":
                sid = ""
            name = raw_name.strip()
            fallback_title = str(raw_title or "")
            slot_num = entry.get("slot")
        else:
            return {"ok": False, "error": "each slot must be a session id"}
        if sid.startswith("-") or (not sid and not name):
            return {"ok": False, "error": "invalid session id"}
        resolved_title = titles.get(sid, fallback_title) if sid else (fallback_title or name)
        item: dict[str, Any] = {
            "id": sid,
            "name": name,
            "title": resolved_title or name,
        }
        if slot_num in (1, 2, 3) and not isinstance(slot_num, bool):
            item["slot"] = int(slot_num)
        elif isinstance(entry, dict) and "slot" in entry:
            return {"ok": False, "error": "slot number must be 1, 2, or 3"}
        slots.append(item)
    try:
        update_settings(state_dir, {"slots": slots})
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    _notify_hub_settings_changed(state_dir)
    return {"ok": True, "saved": True, "slots": slots}


@router.get("/sessions")
def get_sessions() -> dict[str, Any]:
    state_dir = _resolve_state_dir()
    rows = _list_sessions_fast(state_dir)
    return {"ok": True, "sessions": rows}


@router.post("/voice-sample")
def post_voice_sample(body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be an object"}
    voice = body.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        return {"ok": False, "error": "voice is required"}
    voice = voice.strip()
    try:
        parse_voice_spec(voice)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    def _synth() -> bytes:
        try:
            from hail_hermes import kokoro_worker as kw  # type: ignore[attr-defined]

            for fn_name in ("synthesize_sample_wav", "synthesize_wav", "render_sample_wav"):
                fn = getattr(kw, fn_name, None)
                if callable(fn):
                    data = fn(voice=voice, text="Hail Hermes, ready on your wrist.")
                    if isinstance(data, (bytes, bytearray)) and len(data) > 44:
                        return bytes(data)
        except Exception:
            pass
        if _SAMPLE_PATH.is_file():
            try:
                return _SAMPLE_PATH.read_bytes()
            except OSError:
                pass
        return _fallback_wav_bytes()

    try:
        wav_bytes = _run_bounded(_synth, timeout=_ROUTE_BUDGET)
    except Exception:
        wav_bytes = _fallback_wav_bytes()
    return {
        "ok": True,
        "voice": voice,
        "base64": base64.b64encode(wav_bytes).decode("ascii"),
    }


@router.get("/soul")
def get_soul() -> dict[str, Any]:
    state_dir = _resolve_state_dir()
    path = _resolve_soul_path(state_dir)
    default_text = _default_soul_text()
    try:
        content = _run_bounded(path.read_text, encoding="utf-8", timeout=_ROUTE_BUDGET)
        is_default = False
    except (FileNotFoundError, OSError, HubUnavailable):
        content = default_text
        is_default = True
    return {
        "ok": True,
        "soul": content,
        "text": content,
        "default": default_text,
        "is_default": is_default,
        "path": str(path),
    }


@router.post("/soul")
def post_soul(body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be an object"}
    state_dir = _resolve_state_dir()
    path = _resolve_soul_path(state_dir)
    default_text = _default_soul_text()
    if body.get("reset") is True:
        text = default_text
    else:
        raw = body.get("soul") if "soul" in body else body.get("text")
        if not isinstance(raw, str):
            return {"ok": False, "error": "soul text must be a string"}
        text = raw
    try:
        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, text)

        _run_bounded(_write, timeout=_ROUTE_BUDGET)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    _notify_hub_settings_changed(state_dir)
    return {"ok": True, "saved": True, "soul": text, "text": text, "default": default_text}


_FACE_MAX_BYTES = 2 * 1024 * 1024
_FACE_TYPES = {
    "image/png": (b"\x89PNG", "face.png"),
    "image/jpeg": (b"\xff\xd8\xff", "face.jpg"),
}


def _face_dir() -> Path:
    return Store(_resolve_state_dir()).dir


def _face_type_of(raw: bytes) -> str | None:
    for mime, (magic, _name) in _FACE_TYPES.items():
        if raw.startswith(magic):
            return mime
    return None


def _face_path(mime: str) -> Path:
    return _face_dir() / _FACE_TYPES[mime][1]


@router.post("/face")
def post_face(body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be an object"}
    data = body.get("data")
    mime = body.get("type")
    if not isinstance(data, str) or not data:
        return {"ok": False, "error": "data must be a base64 string"}
    if mime not in _FACE_TYPES:
        return {"ok": False, "error": "type must be image/png or image/jpeg"}
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return {"ok": False, "error": "That picture could not be read. Please try another file."}
    if not raw:
        return {"ok": False, "error": "That picture is empty. Please try another file."}
    if len(raw) > _FACE_MAX_BYTES:
        return {"ok": False, "error": "That picture is too big. Please use one under 2 MB."}
    magic = _FACE_TYPES[mime][0]
    if not raw.startswith(magic):
        return {"ok": False, "error": "That file does not look like the picture type you chose."}
    try:
        def _write() -> None:
            directory = _face_dir()
            other_name = _FACE_TYPES["image/jpeg" if mime == "image/png" else "image/png"][1]
            atomic_write(_face_path(mime), base64.b64encode(raw).decode("ascii"))
            try:
                (directory / other_name).unlink()
            except FileNotFoundError:
                pass

        _run_bounded(_write, timeout=_ROUTE_BUDGET)
    except Exception as exc:
        return {"ok": False, "error": str(exc) or "Could not save the face."}
    return {"ok": True}


@router.get("/face")
def get_face() -> dict[str, Any]:
    directory = _face_dir()
    for mime, (_magic, name) in _FACE_TYPES.items():
        path = directory / name
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        return {"ok": True, "data": base64.b64encode(raw).decode("ascii"), "type": mime}
    return {"ok": True, "data": None}


@router.post("/face/reset")
def post_face_reset() -> dict[str, Any]:
    directory = _face_dir()
    removed = False
    for _mime, (_magic, name) in _FACE_TYPES.items():
        try:
            (directory / name).unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "removed": removed}


@router.get("/soul-templates")
def get_soul_templates() -> dict[str, Any]:
    templates_path = Path(__file__).resolve().parents[1] / "content" / "souls.json"
    try:
        raw = _run_bounded(templates_path.read_text, encoding="utf-8", timeout=_ROUTE_BUDGET)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("not a list")
        templates = []
        for item in data:
            if not isinstance(item, dict):
                continue
            templates.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "soul": item.get("soul"),
                }
            )
    except Exception:
        return {"ok": False, "templates": [], "error": "Could not load the personalities. Try again."}
    return {"ok": True, "templates": templates}


@router.get("/memory")
def get_memory() -> dict[str, Any]:
    state_dir = _resolve_state_dir()
    try:
        all_lines, path = _run_bounded(_read_memory_lines, state_dir, timeout=_ROUTE_BUDGET)
    except (FileNotFoundError, OSError, HubUnavailable):
        all_lines, path = [], _memory_file(state_dir)
    tail = all_lines[-20:]
    return {
        "ok": True,
        "lines": tail,
        "memory": "\n".join(tail),
        "path": str(path),
    }


@router.post("/memory")
def post_memory(body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be an object"}
    raw_fact = body.get("fact") if "fact" in body else body.get("text")
    if not isinstance(raw_fact, str) or not raw_fact.strip():
        return {"ok": False, "error": "fact must be a non-empty string"}
    state_dir = _resolve_state_dir()
    try:
        _run_bounded(append_fact, raw_fact.strip(), state_dir, timeout=_ROUTE_BUDGET)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    res = get_memory()
    return {**res, "ok": True, "saved": True}


@router.post("/revoke")
def post_revoke(body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be an object"}
    device_id = body.get("device_id") or body.get("id")
    if not isinstance(device_id, str) or not device_id.strip():
        return {"ok": False, "error": "device_id is required"}
    device_id = device_id.strip()
    state_dir = _resolve_state_dir()
    revoked = False
    try:
        client = _open_client(state_dir)
        if _read_live(client):
            _control_call(client, "revoke", device_id=device_id)
            revoked = True
    except Exception:
        pass
    try:
        store = Store(state_dir)
        if hasattr(store, "revoke"):
            _run_bounded(store.revoke, device_id, timeout=_ROUTE_BUDGET)
            revoked = True
    except Exception as exc:
        if not revoked:
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "saved": True, "device_id": device_id}


@router.post("/restart-hub")
def restart_hub() -> dict[str, Any]:
    state_dir = _resolve_state_dir()
    directory = Store(state_dir).dir
    pid = _live_pid(state_dir)
    if pid is not None:
        if "hail_hermes" not in _pid_cmdline(pid):
            return {"ok": False, "error": "live.json pid is not a hail_hermes process"}
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if _PROC_ROOT == Path("/proc"):
            deadline = time.monotonic() + 0.1
            while time.monotonic() < deadline and (_PROC_ROOT / str(pid)).exists():
                time.sleep(0.02)
        for stale in ("live.json", "autostart.pid"):
            try:
                (directory / stale).unlink()
            except FileNotFoundError:
                pass
    with _AUTOSTART_LOCK:
        _AUTOSTART_CACHE["started"] = False
        _AUTOSTART_CACHE["error"] = None
    starting = _kick_ensure_hub(state_dir, wait=0.05)
    with _AUTOSTART_LOCK:
        started = bool(_AUTOSTART_CACHE.get("started"))
        error = _AUTOSTART_CACHE.get("error")
    if error and not starting:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "started": started}
