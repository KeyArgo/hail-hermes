"""Hail Hermes hub + Hermes plugin.

Import cost is kept near zero: Hermes imports this module at plugin discovery, so heavy modules
(websockets server, audio) load only when used."""

__version__ = "1.0.0"


def register(ctx):  # Hermes plugin entry point (directory plugin and pip entry point)
    from .hermes_plugin import register as _register

    _register(ctx)
