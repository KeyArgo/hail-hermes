"""The build must be able to say which build it is."""

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "hail_hermes"
ROOT = Path(__file__).resolve().parents[2]


def test_the_stamp_is_vendored_in_the_package():
    assert (PACKAGE / "version_stamp.py").is_file()


def test_the_stamp_never_invents_a_commit():
    import sys

    sys.path.insert(0, str(ROOT / "plugin"))
    from hail_hermes import version_stamp

    info = version_stamp.as_dict()
    assert info["version"]
    assert info["source"] in {"build_info", "git", "manifest"}
    if info["source"] == "manifest":
        assert info["commit"] == ""


def test_the_version_route_carries_the_whole_identity():
    import sys

    import pytest

    # The dashboard module imports FastAPI. Without it this test used to fail when run alone and
    # pass when another test happened to have loaded things first — an order-dependent test is worse
    # than a skipped one, so skip explicitly and say why.
    pytest.importorskip("fastapi", reason="dashboard route needs FastAPI installed")
    sys.path.insert(0, str(ROOT / "plugin"))
    from hail_hermes.dashboard import plugin_api

    answer = plugin_api.version_route()
    assert {"version", "commit", "commit_date", "built_at", "dirty", "channel"} <= set(answer)


def test_the_pane_carries_the_build_stamp_and_the_status_bar_does_not():
    """One stamp, in the pane header and Settings — not a second status-bar item.

    Two copies of the same line in one status bar is clutter, and the pane is where someone looks
    when asking "which build am I running". This test pins the decision so a future change has to be
    deliberate rather than accidental.
    """
    bundle = (PACKAGE / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "HailVersionChip" in bundle
    assert "/v1/version" in bundle
    assert "id: 'version'" not in bundle, "the build stamp must not register a status-bar item again"


def test_build_info_is_not_committed():
    assert "BUILD_INFO.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_every_manifest_agrees_on_the_release_target():
    """Three manifests and the package's __version__ are one number."""
    import re

    def declared(path: Path) -> str:
        match = re.search(r'^version:\s*"?([^"\s]+)"?', path.read_text(encoding="utf-8"), re.M)
        assert match, f"no version in {path}"
        return match.group(1)

    targets = {
        "plugin.yaml": declared(ROOT / "plugin.yaml"),
        "plugin/plugin.yaml": declared(ROOT / "plugin" / "plugin.yaml"),
        "plugin/hail_hermes/plugin.yaml": declared(ROOT / "plugin" / "hail_hermes" / "plugin.yaml"),
    }
    pyproject = re.search(
        r'^version\s*=\s*"([^"]+)"',
        (ROOT / "plugin" / "pyproject.toml").read_text(encoding="utf-8"),
        re.M,
    )
    assert pyproject, "no version in plugin/pyproject.toml"
    targets["plugin/pyproject.toml"] = pyproject.group(1)

    import sys

    sys.path.insert(0, str(ROOT / "plugin"))
    from hail_hermes import __version__

    targets["hail_hermes.__version__"] = __version__
    assert len(set(targets.values())) == 1, targets



def _manifest_description(path: Path) -> str:
    """The `description:` block of a manifest, folded to one line for comparison."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("description:"):
            inside = True
            rest = line.split(":", 1)[1].strip()
            out.append(rest.lstrip(">|-").strip())
            continue
        if inside:
            if not line.strip() or line[:1] not in " \t":
                break
            out.append(line.strip())
    return " ".join(part for part in out if part)


def test_the_three_manifests_tell_one_story():
    """The repo carries three manifests; a description edited in one and not the others is drift.

    This exists because it happened: the panel-first rewrite landed in the package manifest and the
    two manifests Hermes and the catalog actually read kept saying "your agent on your wrist".
    """
    manifests = [ROOT / "plugin.yaml", ROOT / "plugin" / "plugin.yaml", PACKAGE / "plugin.yaml"]
    for path in manifests:
        assert path.is_file(), f"missing manifest: {path}"
    descriptions = {_manifest_description(path) for path in manifests}
    assert len(descriptions) == 1, "manifest descriptions disagree:\n" + "\n".join(sorted(descriptions))
    only = descriptions.pop()
    assert "desktop panel" in only, "the description must lead with the panel, not the wrist"
