"""The pane file at the plugin root must BE the pane, not a pointer to it.

Hermes loads a plugin's desktop half from `<plugin>/desktop/plugin.js` and forbids any import except
`@hermes/plugin-sdk` and `react`. A re-export shim (`export * from '../plugin/hail_hermes/...'`) is
therefore rejected at load time with "unsupported import", the pane never mounts, and the status-bar
chip silently disappears — which is exactly what happened after upgrading to 0.2.0.

So the root file is a byte-for-byte copy of the canonical module, and this test is what stops the two
from drifting. Fix a drift with `bash tools/sync-desktop-half.sh`.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "plugin" / "hail_hermes" / "desktop" / "plugin.js"
ROOT_HALF = ROOT / "desktop" / "plugin.js"


def test_root_half_exists() -> None:
    assert ROOT_HALF.is_file(), "the plugin root has no desktop/plugin.js — the pane will not mount"


def test_root_half_is_a_real_module_not_a_reexport() -> None:
    text = ROOT_HALF.read_text(encoding="utf-8")
    assert "export * from" not in text, (
        "the root desktop half must not re-export: Hermes rejects any import other than "
        "@hermes/plugin-sdk and react, and the pane fails to load"
    )
    assert "from '../plugin/" not in text, "the root desktop half must not import from the package"


def test_root_half_matches_the_canonical_module() -> None:
    assert ROOT_HALF.read_bytes() == CANONICAL.read_bytes(), (
        "desktop/plugin.js has drifted from plugin/hail_hermes/desktop/plugin.js — run "
        "`bash tools/sync-desktop-half.sh`"
    )
