"""Synchronous client for the hub's loopback ``/control`` channel.

Used by the Hermes device tools (which run inside the agent process, often a separate process
from the hub) and by ``pocket-hub notify|say|devices``. Discovery is via ``live.json`` +
``control.token`` in the hub state dir, so no ports or secrets need configuring."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .store import Store


class HubUnavailable(RuntimeError):
    pass


class ControlClient:
    def __init__(self, state_dir: Path | str | None = None, timeout: float = 1.0):
        self.store = Store(state_dir)
        self.timeout = timeout

    def live(self) -> dict[str, Any] | None:
        return self.store.read_live()

    def connected(self) -> list[dict[str, Any]]:
        """Connected devices from the hub's live snapshot (cheap: one file read, no socket)."""
        live = self.live()
        return list(live.get("devices", [])) if live else []

    def has_cap(self, cap: str | None) -> bool:
        return any(cap is None or d.get("caps", {}).get(cap) for d in self.connected())

    def pair_list(self) -> list[dict[str, Any]]:
        return self.call("pair_list")

    def pair_decide(self, decision: str, identifier: str) -> dict[str, Any]:
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be approve or deny")
        key = "match_code" if len(identifier) == 4 and identifier.isascii() and identifier.isdigit() else "request_id"
        return self.call(f"pair_{decision}", **{key: identifier})

    def revoke(self, device_id: str) -> dict[str, Any]:
        return self.call("revoke", device_id=device_id)

    def call(self, op: str, **args: Any) -> Any:
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect

        live = self.live()
        token = self.store.control_token(create=False)
        if not live or not token:
            raise HubUnavailable("Hail Hermes hub is not running (start it with `pocket-hub serve` or `hermes hail serve`)")
        req_id = uuid.uuid4().hex
        try:
            with connect(live["control_url"], additional_headers={"Authorization": f"Bearer {token}"},
                         open_timeout=self.timeout, close_timeout=min(1.0, self.timeout)) as ws:
                ws.send(json.dumps({"id": req_id, "op": op, "args": args}))
                reply = json.loads(ws.recv(timeout=self.timeout))
        # A hub that is restarting can drop the handshake or the reply; that is "unavailable", not a crash.
        except (OSError, TimeoutError, WebSocketException, ValueError) as exc:
            raise HubUnavailable(f"Hail Hermes hub unreachable at {live.get('control_url')}: {exc}") from None
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "control call failed")
        return reply.get("result")
