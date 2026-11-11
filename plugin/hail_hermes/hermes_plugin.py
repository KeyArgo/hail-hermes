"""Hermes plugin entry: ``register(ctx)`` wires the device tools and the ``hermes hail`` CLI."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from .control import ControlClient
from .tools import TOOLS, TOOLSET, make_check, make_handler

log = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    state_dir = None
    try:
        state_dir = ctx.get_config("state_dir", default="") or None
    except Exception:
        pass
    client = ControlClient(state_dir)
    for name, (cap, op, schema) in TOOLS.items():
        ctx.register_tool(
            name=name, toolset=TOOLSET, schema=schema, handler=make_handler(op, client),
            check_fn=make_check(cap, client), description=schema["description"], emoji="⌚",
        )
    try:
        from .cli import add_subcommands, dispatch

        ctx.register_cli_command(
            name="hail", help="Hail Hermes hub (phone/watch companion)",
            setup_fn=add_subcommands, handler_fn=dispatch,
            description="Run Hail Hermes and manage paired devices.",
        )
        ctx.register_cli_command(
            name="pocket", help=argparse.SUPPRESS,
            setup_fn=add_subcommands, handler_fn=dispatch,
            description="Compatibility alias for hermes hail.",
        )
    except Exception:
        log.debug("hail-hermes: CLI command registration failed", exc_info=True)
