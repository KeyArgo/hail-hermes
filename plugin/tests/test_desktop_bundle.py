"""The desktop bundle must be loadable by the app's runtime plugin loader.

Hermes Desktop does not bundle runtime plugins: it reads the plugin's `plugin.js` as source text,
collects every import specifier, and refuses the plugin outright if anything other than
`@hermes/plugin-sdk` and `react` appears. A re-export shim — `export * from '../plugin/.../plugin.js'`
— is therefore not a shortcut but a hard load failure:

    [plugins] runtime load failed (hail-hermes) Error: unsupported import:
    ../plugin/hail_hermes/desktop/plugin.js

Both copies must hold the real module, byte for byte. This test is the thing that stops someone
"tidying" the duplicate back into a shim.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "plugin" / "hail_hermes" / "desktop" / "plugin.js"
DEPLOYED = ROOT / "desktop" / "plugin.js"
ALLOWED = ("@hermes/plugin-sdk", "react")


def _import_specifiers(source: str) -> set[str]:
    specs: set[str] = set()
    for pattern in (
        r"""import\s+[^;]*?\sfrom\s+['"]([^'"]+)['"]""",
        r"""import\s+['"]([^'"]+)['"]""",
        r"""export\s+[^;]*?\sfrom\s+['"]([^'"]+)['"]""",
        r"""import\(\s*['"]([^'"]+)['"]\s*\)""",
    ):
        specs.update(re.findall(pattern, source, flags=re.S))
    return specs


def test_both_bundle_copies_exist():
    assert CANONICAL.is_file(), f"missing {CANONICAL}"
    assert DEPLOYED.is_file(), f"missing {DEPLOYED}"


def test_the_deployed_bundle_is_the_real_module():
    """A shim here is a load failure in the app, not a convenience."""
    assert DEPLOYED.read_bytes() == CANONICAL.read_bytes(), (
        "desktop/plugin.js has drifted from the canonical module. Copy the real file over it: "
        "the desktop loader rejects relative re-exports."
    )


def test_the_deployed_bundle_imports_only_what_the_loader_allows():
    specs = _import_specifiers(DEPLOYED.read_text(encoding="utf-8"))
    offending = {s for s in specs if not s.startswith(ALLOWED)}
    assert not offending, f"imports the desktop loader rejects: {sorted(offending)}"


def test_the_bundle_fits_the_loaders_read_limit():
    """The shell reads at most 512 KiB of plugin source (see `plugin.js exceeds this shell`)."""
    assert DEPLOYED.stat().st_size < 512 * 1024
