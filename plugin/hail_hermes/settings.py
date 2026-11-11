"""Persistent Pocket settings shared by the CLI and dashboard."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from .kokoro_worker import parse_voice_spec
from . import filelock
from .store import default_state_dir

HERMES_VOICE = "am_puck:0.5,am_adam:0.5"
NOUS_VOICE = "af_heart"

CHARACTERS: dict[str, dict[str, str]] = {
    "hermes": {"id": "hermes", "face": "hermes", "voice": HERMES_VOICE},
    "nous": {"id": "nous", "face": "nous", "voice": NOUS_VOICE},
    "nous-light": {"id": "nous-light", "face": "nous-light", "voice": NOUS_VOICE},
    "nous-dark": {"id": "nous-dark", "face": "nous-dark", "voice": NOUS_VOICE},
    "nous-white": {"id": "nous-white", "face": "nous-white", "voice": NOUS_VOICE},
    "nous-soft": {"id": "nous-soft", "face": "nous-soft", "voice": NOUS_VOICE},
    "nous-teal": {"id": "nous-teal", "face": "nous-teal", "voice": NOUS_VOICE},
    "nous-warm": {"id": "nous-warm", "face": "nous-warm", "voice": NOUS_VOICE},
}

DEFAULTS: dict[str, Any] = {
    "lan_enabled": False,
    "model": "",
    "provider": "",
    "toolsets": "",
    "quick_url": "",
    "quick_backend": "hermes",
    "quick_model": "",
    "quick_endpoints": [],
    "voice_session": "new",
    "pinned_session_id": "",
    "slots": [],
    "stt_threads": os.cpu_count() or 4,
    "soul_path": str(default_state_dir() / "soul.md"),
    "memory_path": "",
    "stt": "auto",
    "tts": "auto",
    "character": "nous-warm",
    # Feature switches: a new watch screen is only reachable when its message is sent,
    # so turning a feature off here is a complete rollback without touching code.
    "features": {"sessions": False, "approval": False, "cards": False},
    # What the watch may change itself. Anything not switched on here answers "not_allowed",
    # so the owner keeps control of the settings that matter.
    "watch_can_change": {"character": True, "voice": True, "model": True, "session": True, "speak": True},
    # Up to five "provider::model" picks the watch may switch between. They must all be
    # models Hermes has already authorised; the hub drops anything else.
    "watch_model_presets": [],

    # Where the watch is, for the weather fast path. Empty means "ask the agent instead".
    "home_place": {},
    "voice_hermes": HERMES_VOICE,
    "voice_nous": NOUS_VOICE,
    # How much faster or slower than normal the speech engine should talk, as edge-tts
    # spells it ("-5%"). A calmer default is easier to follow on a watch speaker.
    "voice_rate": "-5%",
}

CHOICES = {
    "quick_backend": ["hermes", "url"],
    "stt": ["auto", "parakeet", "hermes"],
    "tts": ["auto", "piper", "kokoro", "edge", "none"],
    "voice_session": ["new", "pinned"],
    "character": ["hermes", "nous", "nous-dark", "nous-light", "nous-white", "nous-soft", "nous-teal", "nous-warm"],
}

_LISTENERS: list[Callable[[Path, dict[str, Any]], None]] = []


def add_settings_listener(listener: Callable[[Path, dict[str, Any]], None]) -> None:
    if listener not in _LISTENERS:
        _LISTENERS.append(listener)


def remove_settings_listener(listener: Callable[[Path, dict[str, Any]], None]) -> None:
    if listener in _LISTENERS:
        _LISTENERS.remove(listener)


def _directory(state_dir: str | Path | None) -> Path:
    return Path(state_dir).expanduser() if state_dir else default_state_dir()


def _defaults_for(directory: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in DEFAULTS.items():
        out[key] = list(val) if isinstance(val, list) else val
    out["soul_path"] = str(directory / "soul.md")
    return out


def validate(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if key == "lan_enabled":
            if not isinstance(value, bool):
                raise ValueError("lan_enabled must be a boolean")
            cleaned[key] = value
        elif key == "stt_threads":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("stt_threads must be a positive integer")
            cleaned[key] = value
        elif key == "slots":
            if not isinstance(value, list) or len(value) > 3:
                raise ValueError("slots must be a list of at most 3 items")
            norm_slots: list[dict[str, Any]] = []
            for item in value:
                if not isinstance(item, dict):
                    raise ValueError("each slot must be an object with id and title")
                sid = item.get("id", "")
                if sid is None:
                    sid = ""
                name = item.get("name", "")
                if name is None:
                    name = ""
                title = item.get("title", "")
                if title is None:
                    title = ""
                if not isinstance(sid, str) or sid.strip().startswith("-"):
                    raise ValueError("slot id must be a non-empty session id")
                if not isinstance(name, str):
                    raise ValueError("slot name must be a string")
                if not isinstance(title, str):
                    raise ValueError("slot title must be a string")
                sid = sid.strip()
                name = name.strip()
                if not sid and not name:
                    raise ValueError("slot id must be a non-empty session id")
                entry: dict[str, Any] = {"id": sid, "name": name, "title": title or name}
                if "slot" in item:
                    slot_num = item["slot"]
                    if isinstance(slot_num, bool) or not isinstance(slot_num, int) or slot_num not in (1, 2, 3):
                        raise ValueError("slot number must be 1, 2, or 3")
                    entry["slot"] = slot_num
                norm_slots.append(entry)
            cleaned[key] = norm_slots
        elif key == "features":
            if not isinstance(value, dict):
                raise ValueError("features must be an object")
            unknown = set(value) - set(DEFAULTS["features"])
            if unknown:
                raise ValueError(f"unknown features: {', '.join(sorted(unknown))}")
            if any(not isinstance(flag, bool) for flag in value.values()):
                raise ValueError("feature switches must be true or false")
            cleaned[key] = {**DEFAULTS["features"], **value}
        elif key == "home_place":
            if not isinstance(value, dict):
                raise ValueError("home_place must be an object with label, lat and lon")
            if not value:
                # Empty means "no home town yet": the weather fast path stands down and the
                # agent answers instead. It must be a legal, saveable value.
                cleaned[key] = {}
                continue
            lat, lon = value.get("lat"), value.get("lon")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                raise ValueError("home_place needs numeric lat and lon")
            if not (-90 <= float(lat) <= 90) or not (-180 <= float(lon) <= 180):
                raise ValueError("home_place coordinates are out of range")
            cleaned[key] = {
                "label": str(value.get("label") or "").strip(),
                "lat": float(lat),
                "lon": float(lon),
            }
        elif key == "watch_can_change":
            if not isinstance(value, dict):
                raise ValueError("watch_can_change must be an object")
            unknown = set(value) - set(DEFAULTS["watch_can_change"])
            if unknown:
                raise ValueError(f"unknown watch switches: {', '.join(sorted(unknown))}")
            if any(not isinstance(flag, bool) for flag in value.values()):
                raise ValueError("watch switches must be true or false")
            cleaned[key] = {**DEFAULTS["watch_can_change"], **value}
        elif key == "watch_model_presets":
            if not isinstance(value, list):
                raise ValueError("watch_model_presets must be a list of source::model strings")
            out: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    item = f"{item.get('source', '')}::{item.get('id', '')}"
                if not isinstance(item, str) or "::" not in item:
                    raise ValueError("each preset must look like source::model")
                text = item.strip()
                if text and text not in out:
                    out.append(text)
            if len(out) > 5:
                raise ValueError("at most five presets fit on the watch")
            cleaned[key] = out
        elif key == "voice_rate":
            if not isinstance(value, str) or not re.fullmatch(r"[+-][0-9]{1,2}%", value):
                raise ValueError("voice_rate must look like +5% or -5%")
            cleaned[key] = value
        elif key == "quick_endpoints":
            if not isinstance(value, list) or any(not isinstance(u, str) or not u.strip() for u in value):
                raise ValueError("quick_endpoints must be a list of non-empty URL strings")
            cleaned[key] = [u.strip() for u in value]
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            if key == "pinned_session_id" and value.startswith("-"):
                raise ValueError("invalid pinned_session_id")
            if key in CHOICES and value not in CHOICES[key]:
                raise ValueError(f"invalid {key}")
            if key in ("voice_hermes", "voice_nous") and value.strip():
                parse_voice_spec(value)
            cleaned[key] = value
    return cleaned


def character_info(
    settings: dict[str, Any] | None = None, *, state_dir: str | Path | None = None
) -> dict[str, str]:
    cfg = settings if isinstance(settings, dict) else read_settings(state_dir)
    char_id = str(cfg.get("character") or "hermes").strip().lower()
    if char_id not in CHARACTERS:
        char_id = "hermes"
    base = CHARACTERS[char_id]
    voice = str(cfg.get(f"voice_{char_id}") or "").strip() or base["voice"]
    return {"id": char_id, "face": base["face"], "voice": voice}


def character_label(char_id: str | None) -> str:
    """Friendly name used in session titles and menus (not the raw skin id)."""
    cid = str(char_id or "").strip().lower()
    if cid.startswith("nous"):
        return "Nous"
    if cid == "hermes":
        return "Hermes"
    return (cid or "hermes").capitalize()


def read_settings(state_dir: str | Path | None = None) -> dict[str, Any]:
    directory = _directory(state_dir)
    defaults = _defaults_for(directory)
    path = directory / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaults
    merged = {**defaults, **validate(data)}
    merged["quick_backend"] = "url" if str(merged.get("quick_url") or "").strip() else "hermes"
    if not merged.get("soul_path"):
        merged["soul_path"] = str(directory / "soul.md")
    return merged


def update_settings(
    state_dir: str | Path | None, values: dict[str, Any]
) -> dict[str, Any]:
    """Validate and atomically merge settings under an interprocess lock."""
    updates = validate(values)
    directory = _directory(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(directory / "settings.lock", os.O_RDWR | os.O_CREAT, 0o600)
    merged: dict[str, Any] | None = None
    try:
        filelock.lock(lock_fd)
        merged = {**read_settings(directory), **updates}
        fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(merged, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not hasattr(os, "fchmod"):
                os.chmod(tmp, 0o600)
            os.replace(tmp, directory / "settings.json")
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    finally:
        os.close(lock_fd)
    assert merged is not None
    for listener in list(_LISTENERS):
        try:
            listener(directory.resolve(), dict(merged))
        except Exception:
            pass
    return merged
