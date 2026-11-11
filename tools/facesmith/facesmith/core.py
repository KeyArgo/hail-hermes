"""facesmith — procedural 1-bit mascot frame generator.

Generates the Hermes Pocket mascot as a set of 1-bit (mode "1") PNG frames on a
466x466 canvas, with the face centered in the top two-thirds (round watch).

The mascot is an original, procedurally drawn little robot head: a round head
with an antenna, two round eyes (open or closed for blink), a smile that opens
into mouth shapes for talk, and optional cheek blush for the idle animation.
It is deliberately NOT any existing Nous/Herme mascot (trademark avoided).

Public API:
    SIZE                canvas edge in px (466)
    FRAMES              ordered (filename, role, duration_ms) spec
    MIN_DIFF_PX         minimum required pixel difference per transition (50)
    build_frame(name)   -> (Image["1"], feature_boxes)
    pixel_diff(a, b)    -> int  (count of differing pixels between two frames)
    generate(out_dir)   -> manifest dict (also writes frames/manifest/sheets)
"""

from __future__ import annotations

import json
import math
import os
from math import ceil

from PIL import Image, ImageChops, ImageDraw

__all__ = [
    "SIZE",
    "MIN_DIFF_PX",
    "FRAMES",
    "ROLES",
    "TRANSITIONS",
    "build_frame",
    "feature_boxes",
    "pixel_diff",
    "build_contact_sheet",
    "build_geometry_check",
    "build_manifest",
    "generate",
]

SIZE = 466
MIN_DIFF_PX = 50

# Ordered frame spec: (filename, role, duration_ms).
#   idle  -> two subtle idle frames (a gentle bob)
#   blink -> eyes closed, must read clearly on a round LCD
#   talk  -> four mouth shapes for the speaking state
FRAMES = [
    ("idle_1.png", "idle", 1200),
    ("idle_2.png", "idle", 1200),
    ("blink.png", "blink", 140),
    ("talk_1.png", "talk", 90),
    ("talk_2.png", "talk", 90),
    ("talk_3.png", "talk", 90),
    ("talk_4.png", "talk", 90),
]

ROLES = {name: role for name, role, _ in FRAMES}

# Animation edges whose pixel delta must exceed MIN_DIFF_PX. These are the
# transitions the wear client actually walks: the idle loop, into/out of a
# blink, idle -> speak, and the talk shape cycle (which loops back to talk_1).
TRANSITIONS = [
    ("idle_1.png", "idle_2.png"),
    ("idle_2.png", "blink.png"),
    ("blink.png", "idle_1.png"),
    ("idle_1.png", "talk_1.png"),
    ("talk_1.png", "talk_2.png"),
    ("talk_2.png", "talk_3.png"),
    ("talk_3.png", "talk_4.png"),
    ("talk_4.png", "talk_1.png"),
]


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
def _draw(
    size: int,
    *,
    eyes_open: bool,
    mouth,
    y_off: int = 0,
    antenna_on: bool = True,
    cheeks: bool = False,
):
    """Render one mascot frame in mode '1' (white bg, black ink).

    ``mouth`` is either the string ``"smile"`` (a neutral curved smile) or a
    tuple ``("open", half_h, half_w)`` describing a filled open-mouth oval.

    Returns ``(image, boxes)`` where ``boxes`` maps feature name ->
    ``(x0, y0, x1, y1)`` pixel bounding box (used for the geometry check).
    """
    img = Image.new("1", (size, size), 1)
    d = ImageDraw.Draw(img)

    cx = size // 2
    cy = size // 3                 # face centered in the top 2/3 of the canvas
    R = size * 24 // 100           # head radius
    ln = max(3, size // 110)       # line / outline thickness

    oy = y_off

    # Antenna: a short stalk with a round tip.
    if antenna_on:
        base_y = cy - R + oy
        up = R * 28 // 100
        top_y = base_y - up
        dot = R * 8 // 100
        d.line([(cx, base_y), (cx, top_y)], fill=0, width=ln)
        d.ellipse([cx - dot, top_y - dot, cx + dot, top_y + dot], fill=0)

    # Head: a round robot head.
    d.ellipse([cx - R, cy - R + oy, cx + R, cy + R + oy], outline=0, width=ln)

    # Eyes.
    er = R * 26 // 100
    ex_off = R * 40 // 100
    ey = cy - R * 8 // 100 + oy
    eye_boxes = {}
    for sign, key in ((-1, "eye_left"), (1, "eye_right")):
        exx = cx + sign * ex_off
        box = [exx - er, ey - er, exx + er, ey + er]
        eye_boxes[key] = tuple(box)
        if eyes_open:
            d.ellipse(box, fill=1, outline=0, width=max(2, ln // 2))
            pr = R * 10 // 100
            # pupil sits lower-right for a lively look
            d.ellipse(
                [exx + pr // 2 - pr, ey + pr // 2 - pr, exx + pr // 2 + pr, ey + pr // 2 + pr],
                fill=0,
            )
        else:
            # closed eye: a downward (bottom) arc
            d.arc(box, 0, 180, fill=0, width=ln)

    # Cheek blush (idle animation accent).
    if cheeks:
        cr = R * 7 // 100
        cyk = cy + R * 22 // 100 + oy
        for sign in (-1, 1):
            ccx = cx + sign * R * 62 // 100
            d.ellipse([ccx - cr, cyk - cr, ccx + cr, cyk + cr], fill=0)

    # Mouth.
    mx = cx
    my = cy + R * 45 // 100 + oy
    if mouth == "smile":
        sw = R * 45 // 100
        sh = R * 22 // 100
        d.arc([mx - sw, my - sh, mx + sw, my + sh], 0, 180, fill=0, width=ln)
        mbox = (mx - sw, my - sh, mx + sw, my + sh)
    else:
        _, half_h, half_w = mouth
        mbox = (mx - half_w, my - half_h, mx + half_w, my + half_h)
        d.ellipse(list(mbox), fill=0)

    boxes = {"eye_left": eye_boxes["eye_left"], "eye_right": eye_boxes["eye_right"], "mouth": mbox}
    return img, boxes


def _talk_spec(index: int):
    """Return the open-mouth half-(height, width) for talk_N (index 1..4)."""
    # Fractions of head radius, chosen so every consecutive pair differs clearly.
    half_h_frac = [8, 15, 23, 11][index - 1]
    half_w_frac = [15, 18, 22, 26][index - 1]
    R = SIZE * 24 // 100
    return R * half_h_frac // 100, R * half_w_frac // 100


def build_frame(name: str, size: int = SIZE):
    """Build a single mascot frame by filename. Returns ``(image, boxes)``."""
    if name in ("idle_1.png", "idle_2.png"):
        return _draw(
            size,
            eyes_open=True,
            mouth="smile",
            y_off=0 if name == "idle_1.png" else 4,
            antenna_on=True,
            cheeks=(name == "idle_2.png"),
        )
    if name == "blink.png":
        return _draw(size, eyes_open=False, mouth="smile", antenna_on=True, cheeks=False)
    if name.startswith("talk_") and name.endswith(".png"):
        index = int(name[len("talk_"):-len(".png")])
        half_h, half_w = _talk_spec(index)
        return _draw(size, eyes_open=True, mouth=("open", half_h, half_w), antenna_on=True, cheeks=False)
    raise ValueError(f"unknown frame name: {name!r}")


def feature_boxes(name: str, size: int = SIZE):
    """Bounding boxes for the drawn features of ``name`` (for the geometry check)."""
    return build_frame(name, size)[1]


# --------------------------------------------------------------------------- #
# Frame comparison
# --------------------------------------------------------------------------- #
def pixel_diff(a: Image.Image, b: Image.Image) -> int:
    """Number of pixels that differ between two same-sized images.

    Works for any mode by comparing in grayscale; for the 1-bit frames this is
    exactly the count of pixels that flip between ink and paper.
    """
    la = a.convert("L")
    lb = b.convert("L")
    if la.size != lb.size:
        raise ValueError(f"size mismatch: {la.size} vs {lb.size}")
    diff = ImageChops.difference(la, lb)
    # differing pixels are non-zero; count them via the color histogram.
    # maxcolors >= total pixels guarantees a full histogram (never None).
    colors = diff.getcolors(maxcolors=la.width * la.height + 1)
    assert colors is not None
    return sum(cnt for cnt, col in colors if col)


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def build_manifest(frames_spec=FRAMES, size: int = SIZE, diffs: dict | None = None) -> dict:
    """Assemble the manifest dict (does not write it)."""
    manifest = {
        "generator": "facesmith",
        "format": "1bit",
        "mode": "1",
        "size": [size, size],
        "background": "white",
        "foreground": "black",
        "frames": [
            {"file": name, "role": role, "duration_ms": dur} for name, role, dur in frames_spec
        ],
        "roles": {},
        "diff_threshold_px": MIN_DIFF_PX,
    }
    for role in sorted({r for _, r, _ in frames_spec}):
        manifest["roles"][role] = [n for n, r, _ in frames_spec if r == role]
    if diffs is not None:
        manifest["diffs"] = diffs
    return manifest


# --------------------------------------------------------------------------- #
# Review artifacts
# --------------------------------------------------------------------------- #
def build_contact_sheet(items, size: int = SIZE, cols: int = 4) -> Image.Image:
    """A white RGB sheet laying out every frame with a label, for human review."""
    cell = 160
    pad = 12
    label_h = 24
    title_h = 34
    n = len(items)
    rows = max(1, ceil(n / cols))
    w = cols * cell + (cols + 1) * pad
    h = rows * (cell + label_h) + (rows + 1) * pad + title_h
    sheet = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    d.text((pad, pad), "facesmith contact sheet - 1-bit mascot frames", fill=(0, 0, 0))

    resample = Image.Resampling.NEAREST
    for i, (name, img) in enumerate(items):
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad)
        y = title_h + pad + r * (cell + pad + label_h)
        small = img.convert("L").resize((cell, cell), resample).convert("RGB")
        sheet.paste(small, (x, y))
        d.text((x, y + cell + 4), f"{name}  [{ROLES.get(name, '?')}]", fill=(0, 0, 0))
    return sheet


def build_geometry_check(img: Image.Image, boxes, size: int = SIZE) -> Image.Image:
    """Render ``img`` in RGB with labeled bounding boxes around its features."""
    rgb = img.convert("RGB")
    d = ImageDraw.Draw(rgb)
    ln = max(2, size // 120)
    color = (220, 20, 60)
    labels = [("eye_left", "left eye"), ("eye_right", "right eye"), ("mouth", "mouth")]
    for key, label in labels:
        if key not in boxes:
            continue
        x0, y0, x1, y1 = boxes[key]
        d.rectangle([x0, y0, x1, y1], outline=color, width=ln)
        d.text((x0, max(0, y0 - 14)), f"{label}: {x1 - x0}x{y1 - y0}", fill=color)
    return rgb


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _frame_images(size: int = SIZE):
    images = {}
    boxes = {}
    for name, _, _ in FRAMES:
        img, bx = build_frame(name, size)
        images[name] = img
        boxes[name] = bx
    return images, boxes


def _compute_diffs(images) -> dict:
    diffs = {}
    for a, b in TRANSITIONS:
        diffs[f"{a}->{b}"] = pixel_diff(images[a], images[b])
    return diffs


def generate(
    out_dir: str,
    size: int = SIZE,
    *,
    write_contact: bool = True,
    write_geometry: bool = True,
    verify: bool = True,
) -> dict:
    """Generate every frame + manifest + review artifacts into ``out_dir``.

    Returns the manifest dict. Raises ``AssertionError`` if any transition fails
    to exceed ``MIN_DIFF_PX`` (so a regression can never ship silently).
    """
    os.makedirs(out_dir, exist_ok=True)

    images, boxes = _frame_images(size)

    # Write the 1-bit frames.
    for name, _, _ in FRAMES:
        images[name].save(os.path.join(out_dir, name), format="PNG")

    diffs = _compute_diffs(images)
    if verify:
        for edge, delta in diffs.items():
            assert delta > MIN_DIFF_PX, (
                f"transition {edge} differs by only {delta}px "
                f"(<= {MIN_DIFF_PX}px threshold)"
            )

    manifest = build_manifest(FRAMES, size, diffs=diffs)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    if write_contact:
        items = [(name, images[name]) for name, _, _ in FRAMES]
        build_contact_sheet(items, size).save(os.path.join(out_dir, "contact_sheet.png"))
    if write_geometry:
        build_geometry_check(images["idle_1.png"], boxes["idle_1.png"], size).save(
            os.path.join(out_dir, "geometry_check.png")
        )

    return manifest
