"""Pane backend, at the path the plugin host mounts (`dashboard/manifest.json` → `api`).

The real implementation lives with the Python package (`plugin/hail_hermes/dashboard/plugin_api.py`),
because the pip layout and its 300+ tests import it there. Installing from the source repo puts this
directory at the plugin root, so without this shim the desktop half has no backend: every
`/api/plugins/hail-hermes/*` call returns 500 and the pane renders empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PKG_PARENT = Path(__file__).resolve().parents[1] / "plugin"
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from hail_hermes.dashboard.plugin_api import router  # noqa: E402,F401  (the host mounts this)
