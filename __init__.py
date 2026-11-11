"""Repo-root entry so `hermes plugins install KeyArgo/hail-hermes-dev` works.

The plugin's real code lives in ``plugin/hail_hermes/`` (a pip layout the tests import from). Hermes
installs a *directory* plugin and looks for ``__init__.py`` beside ``plugin.yaml``, so this wrapper
puts the package on the path and re-exports the entry point. Without it the source repo validates as
"nothing to load" and nobody can install straight from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PKG_PARENT = Path(__file__).resolve().parent / "plugin"
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from hail_hermes.hermes_plugin import register  # noqa: E402,F401  (Hermes calls this)
