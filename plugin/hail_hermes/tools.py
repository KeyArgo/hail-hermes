"""Agent-facing device tools (protocol: device_notify / device_say / device_haptic / device_battery).

Handlers follow the Hermes tool contract: ``handler(args: dict, **kwargs) -> str`` (JSON, never
raises). Availability is gated per cap by ``check_fn`` against the hub's live device snapshot, so
a tool is only offered to the model while a device that supports it is connected."""

from __future__ import annotations

import json
from typing import Any, Callable

from .control import ControlClient

TOOLSET = "pocket"

_DEVICE_ID = {
    "type": "string",
    "description": "Target device id. Optional when exactly one capable device is connected.",
}

# name -> (cap required, control op, schema)
TOOLS: dict[str, tuple[str | None, str, dict[str, Any]]] = {
    "device_notify": ("push", "notify", {
        "name": "device_notify",
        "description": ("Push a notification to the user's phone or watch (Hail Hermes). Use for proactive "
                        "reminders and anything the user should see even when not looking at a chat."),
        "parameters": {"type": "object", "properties": {
            "device_id": _DEVICE_ID,
            "title": {"type": "string", "description": "Short notification title"},
            "body": {"type": "string", "description": "Notification text"},
        }, "required": ["title", "body"]},
    }),
    "device_say": (None, "say", {
        "name": "device_say",
        "description": "Speak text aloud on the user's phone/watch (text-to-speech on the device) and show it there.",
        "parameters": {"type": "object", "properties": {
            "device_id": _DEVICE_ID,
            "text": {"type": "string", "description": "What to say"},
        }, "required": ["text"]},
    }),
    "device_haptic": ("haptic", "haptic", {
        "name": "device_haptic",
        "description": "Buzz the user's watch/phone. Use to get attention discreetly.",
        "parameters": {"type": "object", "properties": {
            "device_id": _DEVICE_ID,
            "pattern": {"type": "string", "description": "Vibration pattern: short | long | double | sos",
                        "enum": ["short", "long", "double", "sos"]},
        }, "required": []},
    }),
    "device_battery": ("battery", "battery", {
        "name": "device_battery",
        "description": "Read a connected device's battery: millivolts and whether it is charging.",
        "parameters": {"type": "object", "properties": {"device_id": _DEVICE_ID}, "required": []},
    }),
}

_ARGS = {
    "notify": ("device_id", "title", "body"),
    "say": ("device_id", "text"),
    "haptic": ("device_id", "pattern"),
    "battery": ("device_id",),
}


def make_handler(op: str, client: ControlClient) -> Callable[..., str]:
    def handler(args: dict, **kwargs: Any) -> str:
        try:
            call_args = {k: args[k] for k in _ARGS[op] if args.get(k) not in (None, "")}
            if op == "haptic":
                call_args.setdefault("pattern", "short")
            return json.dumps({"ok": True, **(client.call(op, **call_args) or {})})
        except Exception as exc:  # tool contract: report, never raise
            return json.dumps({"ok": False, "error": str(exc)})

    handler.__name__ = f"pocket_{op}"
    return handler


def make_check(cap: str | None, client: ControlClient) -> Callable[[], bool]:
    def check() -> bool:
        try:
            return client.has_cap(cap)
        except Exception:
            return False

    check.__name__ = f"pocket_has_{cap or 'device'}"
    return check
