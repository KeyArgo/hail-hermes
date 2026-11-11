"""Persistent hub state: paired-device tokens, agent session ids, control secret, live snapshot."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import filelock


def default_state_dir() -> Path:
    env = os.environ.get("HAIL_HERMES_STATE") or os.environ.get("HERMES_POCKET_STATE")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("HERMES_HOME") or "~/.hermes"
    profile = Path(home).expanduser()
    legacy = profile / "pocket"
    return legacy if legacy.is_dir() else profile / "hail-hermes"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Kinds that belong to tools and tests, not to a person's device. A real watch, phone
# or desktop must never be pruned, however long it has been offline.
PRUNABLE_KINDS = frozenset({"sim", "test"})
# A probe that has not connected for a week is debris, not a device anyone remembers.
STALE_AFTER_SECONDS = 7 * 24 * 60 * 60


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Store:
    """``devices.json`` (token hashes, never raw tokens) + ``control.token`` + ``live.json``."""

    def __init__(self, state_dir: Path | str | None = None):
        self.dir = Path(state_dir).expanduser() if state_dir else default_state_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.devices_path = self.dir / "devices.json"
        self.devices_lock_path = self.dir / "devices.lock"
        self.live_path = self.dir / "live.json"
        self.control_token_path = self.dir / "control.token"
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}
        self._devices_stat: tuple[int, int, int, int] | None = None
        with self._locked_devices():
            self._refresh(force=True)

    @contextlib.contextmanager
    def _locked_devices(self):
        """Serialize device state operations across threads and processes."""
        with self._lock:
            fd = os.open(self.devices_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                filelock.lock(fd)
                try:
                    yield
                finally:
                    filelock.unlock(fd)
            finally:
                os.close(fd)

    def _stat_devices(self) -> tuple[int, int, int, int] | None:
        try:
            stat = self.devices_path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.devices_path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            # Keep a corrupt file for inspection rather than silently losing pairings.
            # os.replace, not rename: on Windows rename fails when an older .corrupt exists.
            os.replace(self.devices_path, self.devices_path.with_suffix(".corrupt"))
            return {}
        return data.get("devices", {}) if isinstance(data, dict) else {}

    def _refresh(self, force: bool = False) -> None:
        current = self._stat_devices()
        if force or current != self._devices_stat:
            self._devices = self._load()
            self._devices_stat = current

    def _save(self) -> None:
        atomic_write(self.devices_path, json.dumps({"devices": self._devices}, indent=2))
        self._devices_stat = self._stat_devices()

    def hub_id(self) -> str:
        with self._locked_devices():
            path = self.dir / "hub.id"
            try:
                return path.read_text().strip()
            except FileNotFoundError:
                value = secrets.token_hex(16)
                atomic_write(path, value + "\n")
                return value

    # -- pairing ------------------------------------------------------------
    def issue_token(self, device_id: str, name: str, kind: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._locked_devices():
            self._refresh(force=True)
            entry = self._devices.get(device_id, {})
            entry.update(token_sha256=_hash(token), name=name, kind=kind,
                         paired_at=time.time())
            self._devices[device_id] = entry
            self._save()
        return token

    def check_token(self, device_id: str, token: str | None) -> bool:
        if not token:
            return False
        with self._locked_devices():
            self._refresh(force=True)
            entry = self._devices.get(device_id)
            if not entry or "token_sha256" not in entry:
                return False
            if not hmac.compare_digest(entry["token_sha256"], _hash(token)):
                return False
            entry["last_seen"] = time.time()
            self._save()
            return True

    def prune(self, *, now: float | None = None,
              max_age_seconds: float = STALE_AFTER_SECONDS,
              kinds: frozenset[str] | set[str] = PRUNABLE_KINDS) -> list[str]:
        """Drop offline sim/test devices that have not been seen for ``max_age_seconds``.

        Deliberately narrow, so a forgotten real watch is never lost: only entries whose
        kind is in ``kinds``, that are not in the live snapshot (i.e. not connected), and
        that carry a ``last_seen``/``paired_at`` older than the window are removed. An
        entry with no timestamp is kept. Returns the device ids that were removed.
        """
        now = time.time() if now is None else now
        live = self.read_live() or {}
        connected = {
            row.get("device_id") for row in (live.get("devices") or [])
            if isinstance(row, dict) and row.get("device_id")
        }
        removed: list[str] = []
        with self._locked_devices():
            self._refresh(force=True)
            for device_id, entry in list(self._devices.items()):
                if not isinstance(entry, dict) or device_id in connected:
                    continue
                if entry.get("kind") not in kinds:
                    continue
                seen = entry.get("last_seen", entry.get("paired_at"))
                if not isinstance(seen, (int, float)):
                    continue
                if now - float(seen) >= max_age_seconds:
                    del self._devices[device_id]
                    removed.append(device_id)
            if removed:
                self._save()
        return removed

    def revoke(self, device_id: str) -> bool:
        with self._locked_devices():
            self._refresh(force=True)
            removed = self._devices.pop(device_id, None) is not None
            if removed:
                self._save()
        return removed

    def paired(self) -> dict[str, dict[str, Any]]:
        # Reading the list is the moment old probes would otherwise be shown forever, so
        # that is where the bounded prune runs (the pane and `hermes hail devices` both
        # read through here). Connected devices are skipped inside prune.
        self.prune()
        with self._locked_devices():
            self._refresh()
            return {
                k: {kk: vv for kk, vv in v.items() if kk != "token_sha256"}
                for k, v in self._devices.items()
            }

    # -- agent sessions (device_id -> Hermes session id) ----------------------
    def agent_session(self, device_id: str) -> str | None:
        with self._locked_devices():
            self._refresh()
            return self._devices.get(device_id, {}).get("agent_session_id")

    def set_agent_session(self, device_id: str, session_id: str) -> None:
        with self._locked_devices():
            self._refresh(force=True)
            if device_id in self._devices and session_id:
                self._devices[device_id]["agent_session_id"] = session_id
                self._save()

    def device_mode(self, device_id: str) -> tuple[str | None, str | None]:
        with self._locked_devices():
            self._refresh()
            entry = self._devices.get(device_id, {})
            return entry.get("mode"), entry.get("session_id")

    def device_mode_title(self, device_id: str) -> str | None:
        with self._locked_devices():
            self._refresh()
            return self._devices.get(device_id, {}).get("session_title")

    def set_device_mode(
        self,
        device_id: str,
        mode: str,
        session_id: str | None = None,
        title: str | None = None,
    ) -> None:
        with self._locked_devices():
            self._refresh(force=True)
            entry = self._devices.setdefault(device_id, {})
            entry["mode"] = mode
            if mode == "session":
                if session_id:
                    entry["session_id"] = session_id
                else:
                    entry.pop("session_id", None)
                if title:
                    entry["session_title"] = title
                else:
                    entry.pop("session_title", None)
            else:
                entry.pop("session_id", None)
                entry.pop("session_title", None)
            self._save()

    # -- control channel secret + live snapshot -------------------------------
    def control_token(self, create: bool = True) -> str | None:
        try:
            return self.control_token_path.read_text().strip()
        except FileNotFoundError:
            if not create:
                return None
        token = secrets.token_urlsafe(32)
        atomic_write(self.control_token_path, token)
        return token

    def write_live(self, snapshot: dict[str, Any]) -> None:
        atomic_write(self.live_path, json.dumps(snapshot, indent=2), mode=0o644)

    def read_live(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.live_path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def clear_live(self) -> None:
        try:
            self.live_path.unlink()
        except FileNotFoundError:
            pass
