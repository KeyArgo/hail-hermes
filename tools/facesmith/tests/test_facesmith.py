"""Tests for facesmith: dimensions, mode "1", frame-diff thresholds, manifest.

Run from the repo root:  pytest tools/facesmith
"""

from __future__ import annotations

import json
import os

import pytest
from PIL import Image

import facesmith
from facesmith import (
    FRAMES,
    MIN_DIFF_PX,
    ROLES,
    SIZE,
    TRANSITIONS,
    build_frame,
    build_geometry_check,
    build_contact_sheet,
    generate,
    pixel_diff,
)

EXPECTED_FILES = [name for name, _, _ in FRAMES]


@pytest.fixture(scope="module")
def outdir(tmp_path_factory):
    d = tmp_path_factory.mktemp("mascot")
    generate(str(d), verify=True)
    return d


def _open(path):
    with Image.open(path) as im:
        im.load()
        return im.copy()


# --------------------------------------------------------------------------- #
# Dimensions + 1-bit mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_frame_dimensions_and_mode(name, outdir):
    im = _open(os.path.join(outdir, name))
    assert im.size == (SIZE, SIZE), f"{name} wrong size"
    assert im.mode == "1", f"{name} not 1-bit (mode={im.mode})"


def test_all_expected_frames_written(outdir):
    for name in EXPECTED_FILES:
        assert os.path.isfile(os.path.join(outdir, name)), f"missing frame {name}"
    # exactly the spec frames, no strays (ignoring the review artifacts)
    produced = {f for f in os.listdir(outdir) if f.endswith(".png") and f != "contact_sheet.png" and f != "geometry_check.png"}
    assert produced == set(EXPECTED_FILES)


# --------------------------------------------------------------------------- #
# Frame-diff thresholds (the core "must read on a round LCD" guarantee)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("edge", TRANSITIONS)
def test_transition_diff_above_threshold(edge, outdir):
    a, b = edge
    da = _open(os.path.join(outdir, a))
    db = _open(os.path.join(outdir, b))
    delta = pixel_diff(da, db)
    assert delta > MIN_DIFF_PX, f"{edge} differs by only {delta}px (<= {MIN_DIFF_PX})"


def test_blink_reads_against_idle(outdir):
    """A blink must be clearly visible: eyes open vs closed."""
    idle = _open(os.path.join(outdir, "idle_1.png"))
    blink = _open(os.path.join(outdir, "blink.png"))
    assert pixel_diff(idle, blink) > MIN_DIFF_PX


@pytest.mark.parametrize("name", ["talk_1.png", "talk_2.png", "talk_3.png", "talk_4.png"])
def test_talk_shapes_differ_from_idle(name, outdir):
    idle = _open(os.path.join(outdir, "idle_1.png"))
    talk = _open(os.path.join(outdir, name))
    assert pixel_diff(idle, talk) > MIN_DIFF_PX


def test_recorded_diffs_match_recompute(outdir):
    """The diffs recorded in the manifest are the real pixel deltas."""
    manifest = json.loads((outdir / "manifest.json").read_text())
    imgs = {n: _open(os.path.join(outdir, n)) for n in EXPECTED_FILES}
    for a, b in TRANSITIONS:
        key = f"{a}->{b}"
        assert manifest["diffs"][key] == pixel_diff(imgs[a], imgs[b])
        assert manifest["diffs"][key] > MIN_DIFF_PX


# --------------------------------------------------------------------------- #
# Manifest completeness
# --------------------------------------------------------------------------- #
def test_manifest_present_and_complete(outdir):
    manifest = json.loads((outdir / "manifest.json").read_text())
    assert manifest["format"] == "1bit"
    assert manifest["mode"] == "1"
    assert manifest["size"] == [SIZE, SIZE]
    assert manifest["diff_threshold_px"] == MIN_DIFF_PX

    files = [f["file"] for f in manifest["frames"]]
    assert files == EXPECTED_FILES

    # every frame has a role and a positive integer duration
    valid_roles = set(ROLES.values())
    for f in manifest["frames"]:
        assert f["role"] in valid_roles
        assert isinstance(f["duration_ms"], int) and f["duration_ms"] > 0

    # roles are grouped correctly
    assert set(manifest["roles"]) == {"idle", "blink", "talk"}
    assert manifest["roles"]["blink"] == ["blink.png"]
    assert set(manifest["roles"]["talk"]) == {f"talk_{i}.png" for i in range(1, 5)}
    assert set(manifest["roles"]["idle"]) == {"idle_1.png", "idle_2.png"}


def test_manifest_diffs_all_pass(outdir):
    manifest = json.loads((outdir / "manifest.json").read_text())
    for key, delta in manifest["diffs"].items():
        assert delta > MIN_DIFF_PX, f"{key} = {delta}px"


# --------------------------------------------------------------------------- #
# Review artifacts
# --------------------------------------------------------------------------- #
def test_contact_sheet_written(outdir):
    path = os.path.join(outdir, "contact_sheet.png")
    assert os.path.isfile(path)
    with Image.open(path) as im:
        im.load()
        assert im.mode == "RGB"
        assert im.width > im.height  # a horizontal sheet
        assert im.width > 100 and im.height > 100


def test_geometry_check_written(outdir):
    path = os.path.join(outdir, "geometry_check.png")
    assert os.path.isfile(path)
    with Image.open(path) as im:
        im.load()
        assert im.mode == "RGB"
        assert im.size == (SIZE, SIZE)


def test_geometry_boxes_cover_features(outdir):
    """The geometry check must locate real features inside the canvas."""
    boxes = build_frame("idle_1.png")[1]
    for key in ("eye_left", "eye_right", "mouth"):
        assert key in boxes
        x0, y0, x1, y1 = boxes[key]
        assert 0 <= x0 < x1 <= SIZE
        assert 0 <= y0 < y1 <= SIZE
    # the two eyes are side by side
    assert boxes["eye_left"][2] < boxes["eye_right"][0]


# --------------------------------------------------------------------------- #
# Build-from-scratch + API surface
# --------------------------------------------------------------------------- #
def test_build_frame_returns_mode1_and_boxes():
    for name in EXPECTED_FILES:
        im, boxes = build_frame(name)
        assert im.mode == "1" and im.size == (SIZE, SIZE)
        assert "eye_left" in boxes and "mouth" in boxes


def test_generate_is_deterministic(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    ma = generate(str(a))
    mb = generate(str(b))
    assert ma == mb
    ia = _open(os.path.join(a, "blink.png"))
    ib = _open(os.path.join(b, "blink.png"))
    assert pixel_diff(ia, ib) == 0  # identical bytes -> identical pixels


def test_public_api_exports():
    for sym in ("SIZE", "MIN_DIFF_PX", "FRAMES", "generate", "pixel_diff", "build_frame"):
        assert hasattr(facesmith, sym)
