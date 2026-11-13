"""The manifest must declare the runtime dependency the hub actually needs.

Review recommendation: `websockets` was imported by the hub but only declared in
`plugin/pyproject.toml`, which the root installer never reads — so an install could succeed and then
fail at import. The catalog validator also requires a bounded range, because an unbounded extra can pull
a major that changes behaviour under someone who only ran `pip install`.

This fails on the reviewed tree (no `python_dependencies` anywhere in the manifest) and on a declaration
without an upper bound.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = [ROOT / "plugin.yaml", ROOT / "plugin" / "plugin.yaml", ROOT / "plugin" / "hail_hermes" / "plugin.yaml"]


def _declared(text: str) -> list[str]:
    """Requirements under `python_dependencies:` in a manifest, without a YAML parser."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip().startswith("python_dependencies:"))
    except StopIteration:
        return []
    out: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped.startswith("- "):
            if stripped and not stripped.startswith("#"):
                break
            continue
        out.append(stripped[2:].strip().strip('"').strip("'"))
    return out


def test_every_manifest_declares_websockets() -> None:
    for manifest in MANIFESTS:
        if not manifest.is_file():
            continue
        deps = _declared(manifest.read_text(encoding="utf-8"))
        assert any(dep.startswith("websockets") for dep in deps), (
            f"{manifest.relative_to(ROOT)} does not declare websockets, which the hub imports"
        )


def test_the_declared_range_is_bounded() -> None:
    for manifest in MANIFESTS:
        if not manifest.is_file():
            continue
        for dep in _declared(manifest.read_text(encoding="utf-8")):
            name = re.split(r"[<>=!~ ]", dep, 1)[0]
            if name != "websockets":
                continue
            assert "<" in dep, (
                f"{manifest.relative_to(ROOT)} declares {dep!r} with no upper bound; the validator "
                "rejects an unbounded dependency"
            )


def test_the_manifest_and_the_package_agree() -> None:
    pyproject = (ROOT / "plugin" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"websockets([^"]*)"', pyproject)
    assert match, "plugin/pyproject.toml no longer pins websockets"
    package_range = match.group(1)
    for manifest in MANIFESTS:
        if not manifest.is_file():
            continue
        for dep in _declared(manifest.read_text(encoding="utf-8")):
            if dep.startswith("websockets"):
                assert dep[len("websockets"):] == package_range, (
                    f"{manifest.relative_to(ROOT)} declares {dep!r} but the package declares "
                    f"websockets{package_range!r} — one version range, not two"
                )
