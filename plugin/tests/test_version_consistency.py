"""The version a person sees must be the version that is running.

Three places can disagree and each has already caused real confusion in this project: the manifest,
the package `__version__` that `/status` reports, and the stamp the pane draws. A hand-bumped pane
chip drifted from the manifest, and a 0.2.0 install reported 0.1.0 from the hub.

The pane therefore holds **no** version constant at all: it asks the backend. These tests pin that
rule, because the tempting fix for "the chip looks wrong" is to hard-code the number again.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANE = ROOT / "plugin" / "hail_hermes" / "desktop" / "plugin.js"


def _manifest_version() -> str:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "plugin.yaml has no version"
    return match.group(1)


def _package_version() -> str:
    text = (ROOT / "plugin" / "hail_hermes" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "package has no __version__"
    return match.group(1)


def test_manifest_and_package_agree() -> None:
    versions = {"plugin.yaml": _manifest_version(), "__init__": _package_version()}
    assert len(set(versions.values())) == 1, f"versions disagree: {versions}"


def test_the_pane_reads_the_version_from_the_backend() -> None:
    text = PANE.read_text(encoding="utf-8")
    assert "/v1/version" in text, "the pane must read /v1/version"
    assert "build.version" in text, "the chip must render the version the backend reports"


def test_the_pane_carries_no_compiled_version_constant() -> None:
    """A constant in the bundle is the drift: it keeps advertising a release after the manifest moves."""
    text = PANE.read_text(encoding="utf-8")
    assert "PANE_VERSION" not in text, "no compiled version constant — read it from the backend"
    assert "PANE_BUILD" not in text, "no compiled build constant — the backend stamps the build"


def test_exactly_one_chip_renders_a_version() -> None:
    """Two version strings in one pane is the defect; one component, reused, is the fix."""
    text = PANE.read_text(encoding="utf-8")
    assert text.count("HailVersionChip") >= 3, "the shared chip component must be reused, not reimplemented"
    assert "build ${PANE_BUILD}" not in text, "Settings must not print its own build line"


def test_the_stamp_uses_the_house_format() -> None:
    """v1.1.2 · 2026-11-11 · d548b101 — version, date and short commit, no abbreviations.

    The stamp exists to be compared with other plugins' stamps and with the catalog pin, so a
    shortened or rearranged line defeats its only purpose.
    """
    text = PANE.read_text(encoding="utf-8")
    assert "' · '" in text, "the parts must be joined with the house separator"
    assert "build.commit_date" in text and "slice(0, 10)" in text, "the date, day-precision"
    assert "slice(0, 8)" in text, "the short commit, eight characters"
    assert "ui-monospace" in text, "the stamp is code-shaped: monospace, like the other chips"
