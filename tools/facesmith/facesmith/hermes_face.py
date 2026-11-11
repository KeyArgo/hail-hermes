"""facesmith.hermes_face — procedural 1-bit Hermes portrait frame generator.

Generates an original animated 1960s graphic-novel ink portrait of the Greek god
Hermes as a set of 1-bit (mode "1") PNG frames on a 466x466 canvas, with the
face centered in the top two-thirds for a round OLED smartwatch.

Style & Subject:
    - Bold white-on-black comic-book ink chiaroscuro (glows on an OLED display).
    - Drawn at 4x resolution using custom-sampled cubic/quadratic Bezier curves
      and tapered brush-stroke polygons, then downsampled and thresholded for
      razor-sharp 1-bit edges that remain legible at 200px wide.
    - Young, handsome, friendly Hermes in a subtle 3/4 turn wearing a winged
      petasos helmet (rounded dome, curved brim, two sweeping wings each with
      5 distinct primary feathers), curly hair under the brim, strong brows,
      straight Greek nose, and a calm confident smile that animates through
      progressive speaking mouth shapes.

Public API:
    SIZE                canvas edge in px (466)
    SCALE               supersampling factor (4)
    MIN_DIFF_PX         minimum required pixel difference per transition (50)
    FRAMES              ordered (filename, role, duration_ms) spec
    ROLES               mapping of filename -> role
    TRANSITIONS         ordered pairs of frame filenames verified by generate()
    LAYOUT              tweakable 1x coordinate & proportion dictionary
    draw_helmet         draws the petasos dome, band, and curved brim
    draw_wing           draws one feathered wing (5 distinct primary feathers)
    draw_hair           draws classical curly locks under the brim and temples
    draw_face           draws the head, jaw, neck, cheekbones, brow, and nose
    draw_eyes           draws open almond eyes with catchlights or closed lids
    draw_mouth          draws the calm confident smile or progressive talk mouths
    build_frame(name)   -> (Image["1"], feature_boxes)
    feature_boxes(name) -> dict of feature bounding boxes
    pixel_diff(a, b)    -> int (count of differing pixels between two frames)
    build_contact_sheet -> RGB review contact sheet Image
    build_geometry_check-> RGB feature bounding-box overlay Image
    build_manifest      -> manifest dict
    generate(out_dir)   -> manifest dict (writes PNG frames, manifest, sheets)
"""

from __future__ import annotations

import argparse
import json
import math
import os
from math import ceil
from typing import Iterable, Sequence

from PIL import Image, ImageChops, ImageDraw

__all__ = [
    "SIZE",
    "SCALE",
    "THRESHOLD",
    "MIN_DIFF_PX",
    "FRAMES",
    "ROLES",
    "TRANSITIONS",
    "LAYOUT",
    "draw_helmet",
    "draw_wing",
    "draw_hair",
    "draw_face",
    "draw_eyes",
    "draw_mouth",
    "build_frame",
    "feature_boxes",
    "pixel_diff",
    "build_contact_sheet",
    "build_geometry_check",
    "build_manifest",
    "generate",
]

SIZE = 466
SCALE = 4
THRESHOLD = 124
MIN_DIFF_PX = 50

# Ink palette in 8-bit grayscale supersampling buffer (0 = black bg, 255 = white ink)
BLACK = 0
WHITE = 255

# Ordered frame spec: (filename, role, duration_ms).
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

# Animation edges whose pixel delta must exceed MIN_DIFF_PX.
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

# Easy-to-tweak 1x coordinates (on the 466x466 reference canvas).
# Center (cx, cy) sits in the upper two-thirds so watch complications fit below.
LAYOUT = {
    "cx": 233.0,
    "cy": 168.0,
    # Petasos helmet relative to (cx, cy)
    "dome_rx": 76.0,
    "dome_top_y": -108.0,
    "dome_base_y": -48.0,
    "brim_rx": 100.0,
    "brim_peak_y": -56.0,
    "brim_tip_y": -34.0,
    # Wing roots & 5 primary feather tip offsets (dx, dy) from (cx, cy)
    "wing_root_x": 74.0,
    "wing_root_y": -50.0,
    "wing_feathers": [
        # (root_dx, root_dy, ctrl1_dx, ctrl1_dy, ctrl2_dx, ctrl2_dy, tip_dx, tip_dy, thickness)
        (64.0, -68.0, 108.0, -108.0, 146.0, -124.0, 178.0, -126.0, 14.0),
        (70.0, -59.0, 118.0, -88.0, 156.0, -98.0, 190.0, -96.0, 13.5),
        (74.0, -50.0, 122.0, -68.0, 156.0, -72.0, 186.0, -66.0, 13.0),
        (76.0, -41.0, 118.0, -50.0, 146.0, -48.0, 170.0, -38.0, 12.0),
        (74.0, -33.0, 108.0, -34.0, 130.0, -28.0, 146.0, -14.0, 11.0),
    ],
    # Face & head proportions relative to (cx, cy)
    "temple_x": 56.0,
    "cheek_x": 54.0,
    "jaw_x": 42.0,
    "chin_dx": -2.0,  # subtle 3/4 turn toward viewer's left
    "chin_dy": 82.0,
    "neck_top_y": 68.0,
    "neck_bot_y": 116.0,
    # Eyes relative to (cx, cy)
    "eye_left_dx": -28.0,
    "eye_right_dx": 27.0,
    "eye_dy": -4.0,
    "eye_rx": 16.0,
    "eye_ry": 7.5,
    # Nose relative to (cx, cy)
    "nose_top_dx": -8.0,
    "nose_top_dy": -16.0,
    "nose_tip_dx": -11.0,
    "nose_tip_dy": 22.0,
    # Mouth relative to (cx, cy)
    "mouth_dx": -1.0,
    "mouth_dy": 46.0,
    "mouth_rx": 24.0,
}


# --------------------------------------------------------------------------- #
# Bezier & Comic-Ink Brush Primitives
# --------------------------------------------------------------------------- #
Point = tuple[float, float]


def _quad_bezier(p0: Point, p1: Point, p2: Point, steps: int = 36) -> list[Point]:
    """Sample ``steps + 1`` points along a quadratic Bezier curve."""
    pts: list[Point] = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        x = mt * mt * p0[0] + 2.0 * mt * t * p1[0] + t * t * p2[0]
        y = mt * mt * p0[1] + 2.0 * mt * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _cubic_bezier(
    p0: Point, p1: Point, p2: Point, p3: Point, steps: int = 44
) -> list[Point]:
    """Sample ``steps + 1`` points along a cubic Bezier curve."""
    pts: list[Point] = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        b0 = mt * mt * mt
        b1 = 3.0 * mt * mt * t
        b2 = 3.0 * mt * t * t
        b3 = t * t * t
        x = b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0]
        y = b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1]
        pts.append((x, y))
    return pts


def _chain_beziers(segments: Sequence[Sequence[Point]], steps: int = 36) -> list[Point]:
    """Chain quadratic (3-pt) or cubic (4-pt) Bezier segments into one polyline."""
    out: list[Point] = []
    for seg in segments:
        if len(seg) == 3:
            pts = _quad_bezier(seg[0], seg[1], seg[2], steps=steps)
        elif len(seg) == 4:
            pts = _cubic_bezier(seg[0], seg[1], seg[2], seg[3], steps=steps)
        else:
            raise ValueError(f"Bezier segment must have 3 or 4 points, got {len(seg)}")
        if out:
            out.extend(pts[1:])
        else:
            out.extend(pts)
    return out


def _tapered_stroke(
    draw: ImageDraw.ImageDraw,
    points: Sequence[Point],
    w_start: float,
    w_mid: float,
    w_end: float,
    fill: int = WHITE,
    peak_t: float = 0.5,
) -> None:
    """Draw a comic-book brush stroke with variable width along ``points``."""
    n = len(points)
    if n < 2:
        return

    left_side: list[Point] = []
    right_side: list[Point] = []

    for i, (x, y) in enumerate(points):
        t = i / (n - 1)
        if t <= peak_t and peak_t > 0:
            u = t / peak_t
            # Smooth cosine ease from w_start to w_mid
            s = 0.5 - 0.5 * math.cos(math.pi * u)
            w = w_start + (w_mid - w_start) * s
        else:
            u = (t - peak_t) / max(1e-6, 1.0 - peak_t)
            s = 0.5 - 0.5 * math.cos(math.pi * u)
            w = w_mid + (w_end - w_mid) * s
        half_w = max(0.25, 0.5 * w)

        if i == 0:
            dx = points[1][0] - x
            dy = points[1][1] - y
        elif i == n - 1:
            dx = x - points[n - 2][0]
            dy = y - points[n - 2][1]
        else:
            dx = points[i + 1][0] - points[i - 1][0]
            dy = points[i + 1][1] - points[i - 1][1]

        length = math.hypot(dx, dy)
        if length < 1e-6:
            nx, ny = 0.0, -1.0
        else:
            nx, ny = -dy / length, dx / length

        left_side.append((x + nx * half_w, y + ny * half_w))
        right_side.append((x - nx * half_w, y - ny * half_w))

    poly = left_side + list(reversed(right_side))
    draw.polygon(poly, fill=fill)

    # Round endcaps when start or end width is substantial
    for (x, y), w_cap in ((points[0], w_start), (points[-1], w_end)):
        r = 0.5 * w_cap
        if r >= 1.2:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


# --------------------------------------------------------------------------- #
# Named Feature Drawing Functions
# --------------------------------------------------------------------------- #
def draw_wing(
    draw: ImageDraw.ImageDraw,
    cx: float = LAYOUT["cx"] * SCALE,
    cy: float = LAYOUT["cy"] * SCALE,
    s: float = float(SCALE),
    side: int = -1,
    wing_shift: float = 0.0,
) -> tuple[int, int, int, int]:
    """Draw one winged-petasos wing with 5 distinct primary feathers.

    ``side`` is ``-1`` for the viewer's left wing or ``+1`` for the right wing.
    ``wing_shift`` (0.0 in idle_1, 1.0 in idle_2) elevates and flares the tips.
    Returns the ``(x0, y0, x1, y1)`` bounding box in canvas coordinates.
    """
    feathers = LAYOUT["wing_feathers"]
    xs: list[float] = []
    ys: list[float] = []

    # Draw from lowest feather (index 4) to top primary feather (index 0)
    # so upper primaries overlap lower ones cleanly like a real bird wing.
    for idx in range(len(feathers) - 1, -1, -1):
        r_dx, r_dy, c1_dx, c1_dy, c2_dx, c2_dy, t_dx, t_dy, thick = feathers[idx]

        # Apply subtle upward & outward animation shift in idle_2
        lift = wing_shift * (9.5 - idx * 1.1)
        flare = wing_shift * (3.5 - idx * 0.4)

        p0 = (cx + side * r_dx * s, cy + r_dy * s)
        p1 = (cx + side * (c1_dx + flare * 0.4) * s, cy + (c1_dy - lift * 0.5) * s)
        p2 = (cx + side * (c2_dx + flare * 0.8) * s, cy + (c2_dy - lift * 0.85) * s)
        p3 = (cx + side * (t_dx + flare) * s, cy + (t_dy - lift) * s)

        spine = _cubic_bezier(p0, p1, p2, p3, steps=40)
        for px, py in spine:
            xs.append(px)
            ys.append(py)

        # 1. Bold black separation halo so adjacent feathers never merge at 200px
        _tapered_stroke(
            draw,
            spine,
            w_start=(thick + 8.5) * s,
            w_mid=(thick + 9.5) * s,
            w_end=5.5 * s,
            fill=BLACK,
            peak_t=0.45,
        )

        # 2. Bright white sculpted feather blade
        _tapered_stroke(
            draw,
            spine,
            w_start=thick * s,
            w_mid=(thick + 1.5) * s,
            w_end=2.2 * s,
            fill=WHITE,
            peak_t=0.48,
        )

        # 3. Graphic-novel black rachis (quill shaft) & lower-vane shadow cut
        quill_pts = _cubic_bezier(
            (p0[0] + side * 4.0 * s, p0[1] + 2.0 * s),
            (p1[0] + side * 2.0 * s, p1[1] + 2.8 * s),
            (p2[0], p2[1] + 2.2 * s),
            (
                p2[0] * 0.28 + p3[0] * 0.72,
                (p2[1] * 0.28 + p3[1] * 0.72) + 1.5 * s,
            ),
            steps=32,
        )
        _tapered_stroke(
            draw,
            quill_pts,
            w_start=3.6 * s,
            w_mid=3.2 * s,
            w_end=1.0 * s,
            fill=BLACK,
            peak_t=0.4,
        )

    # Wing covert shoulder scales where the wing joins the helmet side
    root_x = cx + side * LAYOUT["wing_root_x"] * s
    root_y = cy + LAYOUT["wing_root_y"] * s
    for k, (ox, oy, rad_x, rad_y) in enumerate(
        [(0.0, -8.0, 14.0, 10.0), (side * 8.0, 1.0, 13.0, 9.0)]
    ):
        scx = root_x + ox * s
        scy = root_y + (oy - wing_shift * 2.0) * s
        arc_pts = _quad_bezier(
            (scx - side * 8.0 * s, scy - rad_y * s),
            (scx + side * rad_x * s, scy),
            (scx - side * 6.0 * s, scy + rad_y * s),
            steps=24,
        )
        _tapered_stroke(
            draw, arc_pts, w_start=7.5 * s, w_mid=9.0 * s, w_end=6.5 * s, fill=BLACK
        )
        _tapered_stroke(
            draw, arc_pts, w_start=4.0 * s, w_mid=5.2 * s, w_end=3.0 * s, fill=WHITE
        )
        _ = k

    pad = int(8 * s)
    return (
        int(min(xs)) - pad,
        int(min(ys)) - pad,
        int(max(xs)) + pad,
        int(max(ys)) + pad,
    )


def draw_helmet(
    draw: ImageDraw.ImageDraw,
    cx: float = LAYOUT["cx"] * SCALE,
    cy: float = LAYOUT["cy"] * SCALE,
    s: float = float(SCALE),
) -> tuple[int, int, int, int]:
    """Draw Hermes's rounded petasos helmet dome, band, and curved brim."""
    rx = LAYOUT["dome_rx"] * s
    top_y = cy + LAYOUT["dome_top_y"] * s
    base_y = cy + LAYOUT["dome_base_y"] * s

    # 1. Solid black dome silhouette to mask inner wing roots
    dome_arch = _chain_beziers(
        [
            (
                (cx - rx, base_y),
                (cx - rx * 0.96, top_y + 18.0 * s),
                (cx - rx * 0.42, top_y),
                (cx, top_y),
            ),
            (
                (cx, top_y),
                (cx + rx * 0.42, top_y),
                (cx + rx * 0.96, top_y + 18.0 * s),
                (cx + rx, base_y),
            ),
        ],
        steps=40,
    )
    draw.polygon(dome_arch + [(cx + rx, base_y + 16.0 * s), (cx - rx, base_y + 16.0 * s)], fill=BLACK)

    # 2. Bold white outer contour of the petasos dome
    _tapered_stroke(
        draw,
        dome_arch,
        w_start=6.5 * s,
        w_mid=8.5 * s,
        w_end=6.5 * s,
        fill=WHITE,
        peak_t=0.42,
    )

    # 3. 1960s graphic-novel chiaroscuro highlight crescent on upper-left dome
    hl_outer = _cubic_bezier(
        (cx - rx * 0.82, base_y - 10.0 * s),
        (cx - rx * 0.78, top_y + 22.0 * s),
        (cx - rx * 0.30, top_y + 8.0 * s),
        (cx + rx * 0.25, top_y + 10.0 * s),
        steps=36,
    )
    hl_inner = _cubic_bezier(
        (cx + rx * 0.25, top_y + 10.0 * s),
        (cx - rx * 0.18, top_y + 18.0 * s),
        (cx - rx * 0.56, top_y + 34.0 * s),
        (cx - rx * 0.82, base_y - 10.0 * s),
        steps=36,
    )
    draw.polygon(hl_outer + hl_inner, fill=WHITE)

    # Sculptural comic-book ink ribs curving across the dome crown
    for frac in (-0.28, 0.08, 0.42):
        rib = _quad_bezier(
            (cx + rx * frac * 0.55, top_y + 8.0 * s),
            (cx + rx * frac * 0.85, (top_y + base_y) * 0.5),
            (cx + rx * frac, base_y - 6.0 * s),
            steps=24,
        )
        _tapered_stroke(draw, rib, w_start=1.8 * s, w_mid=3.2 * s, w_end=1.5 * s, fill=WHITE)

    # Crown band / fillet just above the brim
    band_curve = _quad_bezier(
        (cx - rx * 0.95, base_y - 4.0 * s),
        (cx, base_y - 18.0 * s),
        (cx + rx * 0.95, base_y - 4.0 * s),
        steps=36,
    )
    _tapered_stroke(draw, band_curve, w_start=3.0 * s, w_mid=4.8 * s, w_end=3.0 * s, fill=WHITE)

    # 4. Wide sweeping curved petasos brim
    brim_rx = LAYOUT["brim_rx"] * s
    brim_peak_y = cy + LAYOUT["brim_peak_y"] * s
    brim_tip_y = cy + LAYOUT["brim_tip_y"] * s

    brim_upper = _chain_beziers(
        [
            (
                (cx - brim_rx, brim_tip_y),
                (cx - brim_rx * 0.65, brim_peak_y - 8.0 * s),
                (cx - brim_rx * 0.25, brim_peak_y),
                (cx, brim_peak_y),
            ),
            (
                (cx, brim_peak_y),
                (cx + brim_rx * 0.25, brim_peak_y),
                (cx + brim_rx * 0.65, brim_peak_y - 8.0 * s),
                (cx + brim_rx, brim_tip_y),
            ),
        ],
        steps=40,
    )
    brim_lower = _chain_beziers(
        [
            (
                (cx + brim_rx, brim_tip_y),
                (cx + brim_rx * 0.62, brim_peak_y + 6.0 * s),
                (cx + brim_rx * 0.25, brim_peak_y + 11.0 * s),
                (cx, brim_peak_y + 11.0 * s),
            ),
            (
                (cx, brim_peak_y + 11.0 * s),
                (cx - brim_rx * 0.25, brim_peak_y + 11.0 * s),
                (cx - brim_rx * 0.62, brim_peak_y + 6.0 * s),
                (cx - brim_rx, brim_tip_y),
            ),
        ],
        steps=40,
    )

    # Black cast-shadow band around the brim so it separates cleanly from dome & curls
    _tapered_stroke(
        draw, brim_upper, w_start=11.0 * s, w_mid=15.0 * s, w_end=11.0 * s, fill=BLACK
    )
    _tapered_stroke(
        draw, brim_lower, w_start=11.0 * s, w_mid=16.0 * s, w_end=11.0 * s, fill=BLACK
    )

    # Solid white lit petasos brim crescent + lower rim line
    draw.polygon(brim_upper + brim_lower, fill=WHITE)
    _tapered_stroke(
        draw, brim_upper, w_start=5.5 * s, w_mid=7.5 * s, w_end=5.5 * s, fill=WHITE
    )

    # Crisp dark bevel line inside the brim for classic comic detail
    bevel_curve = _quad_bezier(
        (cx - brim_rx * 0.84, brim_tip_y - 2.0 * s),
        (cx, brim_peak_y + 4.0 * s),
        (cx + brim_rx * 0.84, brim_tip_y - 2.0 * s),
        steps=36,
    )
    _tapered_stroke(
        draw, bevel_curve, w_start=1.5 * s, w_mid=3.2 * s, w_end=1.5 * s, fill=BLACK
    )

    return (
        int(cx - brim_rx - 8 * s),
        int(top_y - 8 * s),
        int(cx + brim_rx + 8 * s),
        int(brim_tip_y + 12 * s),
    )


def draw_face(
    draw: ImageDraw.ImageDraw,
    cx: float = LAYOUT["cx"] * SCALE,
    cy: float = LAYOUT["cy"] * SCALE,
    s: float = float(SCALE),
    jaw_drop: float = 0.0,
) -> tuple[int, int, int, int]:
    """Draw Hermes's face contour, neck, cheekbones, strong brows, and straight nose."""
    tx = LAYOUT["temple_x"] * s
    ckx = LAYOUT["cheek_x"] * s
    jx = LAYOUT["jaw_x"] * s
    chin_x = cx + LAYOUT["chin_dx"] * s
    chin_y = cy + (LAYOUT["chin_dy"] + jaw_drop) * s
    jd = jaw_drop * s

    # 1. Head & neck solid black silhouette base
    jaw_chain = _chain_beziers(
        [
            # Left temple to left cheekbone
            (
                (cx - tx, cy - 34.0 * s),
                (cx - tx - 2.0 * s, cy - 10.0 * s),
                (cx - ckx, cy + 12.0 * s),
            ),
            # Left cheekbone through left jaw to chin
            (
                (cx - ckx, cy + 12.0 * s),
                (cx - jx - 5.0 * s, cy + 44.0 * s + jd * 0.5),
                (chin_x - 16.0 * s, chin_y - 4.0 * s),
                (chin_x, chin_y),
            ),
            # Chin through right jaw to right cheekbone
            (
                (chin_x, chin_y),
                (chin_x + 18.0 * s, chin_y - 4.0 * s),
                (cx + jx + 6.0 * s, cy + 44.0 * s + jd * 0.5),
                (cx + ckx, cy + 12.0 * s),
            ),
            # Right cheekbone to right temple
            (
                (cx + ckx, cy + 12.0 * s),
                (cx + tx + 2.0 * s, cy - 10.0 * s),
                (cx + tx, cy - 34.0 * s),
            ),
        ],
        steps=32,
    )
    draw.polygon(jaw_chain, fill=BLACK)

    # 2. Heroic neck columns & classical chlamys collar + comic chiaroscuro neck shadow
    neck_top_y = cy + LAYOUT["neck_top_y"] * s
    neck_bot_y = cy + LAYOUT["neck_bot_y"] * s

    left_neck = _quad_bezier(
        (cx - 34.0 * s, neck_top_y),
        (cx - 36.0 * s, (neck_top_y + neck_bot_y) * 0.5),
        (cx - 44.0 * s, neck_bot_y),
        steps=24,
    )
    right_neck = _quad_bezier(
        (cx + 36.0 * s, neck_top_y - 2.0 * s),
        (cx + 38.0 * s, (neck_top_y + neck_bot_y) * 0.5),
        (cx + 46.0 * s, neck_bot_y),
        steps=24,
    )
    _tapered_stroke(draw, left_neck, w_start=5.5 * s, w_mid=6.5 * s, w_end=3.0 * s, fill=WHITE)
    _tapered_stroke(draw, right_neck, w_start=5.5 * s, w_mid=6.5 * s, w_end=3.0 * s, fill=WHITE)

    # Diagonal comic-book light plane on the left side of the neck (leaves deep black chin shadow)
    neck_light = _quad_bezier(
        (cx - 26.0 * s, chin_y + 2.0 * s),
        (cx - 22.0 * s, neck_bot_y - 14.0 * s),
        (cx - 8.0 * s, neck_bot_y + 2.0 * s),
        steps=20,
    )
    _tapered_stroke(draw, neck_light, w_start=2.5 * s, w_mid=4.5 * s, w_end=2.0 * s, fill=WHITE)

    # Classical collar arch at base of neck
    collar_arch = _quad_bezier(
        (cx - 52.0 * s, neck_bot_y - 2.0 * s),
        (cx, neck_bot_y + 16.0 * s),
        (cx + 54.0 * s, neck_bot_y - 2.0 * s),
        steps=32,
    )
    _tapered_stroke(draw, collar_arch, w_start=3.0 * s, w_mid=6.0 * s, w_end=3.0 * s, fill=WHITE)

    # 3. Bold outer jaw & cheekbone brush contour (thicker on the lit left side)
    _tapered_stroke(
        draw,
        jaw_chain,
        w_start=6.0 * s,
        w_mid=8.5 * s,
        w_end=6.0 * s,
        fill=WHITE,
        peak_t=0.45,
    )

    # Lit left cheekbone chiaroscuro highlight plane
    left_cheek_hl = _quad_bezier(
        (cx - ckx + 6.0 * s, cy - 2.0 * s),
        (cx - ckx + 5.0 * s, cy + 16.0 * s),
        (cx - jx + 6.0 * s, cy + 36.0 * s + jd * 0.3),
        steps=24,
    )
    _tapered_stroke(draw, left_cheek_hl, w_start=2.5 * s, w_mid=5.5 * s, w_end=1.8 * s, fill=WHITE)

    # Sculpted right cheekbone contour accent (classic 1960s comic ink line)
    right_cheek_cut = _quad_bezier(
        (cx + ckx - 10.0 * s, cy + 10.0 * s),
        (cx + jx - 4.0 * s, cy + 24.0 * s),
        (cx + jx - 10.0 * s, cy + 36.0 * s + jd * 0.25),
        steps=20,
    )
    _tapered_stroke(draw, right_cheek_cut, w_start=1.8 * s, w_mid=3.8 * s, w_end=1.5 * s, fill=WHITE)

    # 4. Strong, confident eyebrows
    brow_left = _cubic_bezier(
        (cx - 49.0 * s, cy - 15.0 * s),
        (cx - 38.0 * s, cy - 27.0 * s),
        (cx - 21.0 * s, cy - 26.0 * s),
        (cx - 10.0 * s, cy - 18.0 * s),
        steps=32,
    )
    brow_right = _cubic_bezier(
        (cx + 9.0 * s, cy - 18.0 * s),
        (cx + 21.0 * s, cy - 27.0 * s),
        (cx + 39.0 * s, cy - 27.0 * s),
        (cx + 50.0 * s, cy - 14.0 * s),
        steps=32,
    )
    _tapered_stroke(
        draw, brow_left, w_start=2.5 * s, w_mid=8.2 * s, w_end=6.8 * s, fill=WHITE, peak_t=0.65
    )
    _tapered_stroke(
        draw, brow_right, w_start=6.8 * s, w_mid=8.5 * s, w_end=2.5 * s, fill=WHITE, peak_t=0.35
    )

    # 5. Straight classical Greek nose (dorsal bridge, sculpted tip, nostril wing)
    ntop_x = cx + LAYOUT["nose_top_dx"] * s
    ntop_y = cy + LAYOUT["nose_top_dy"] * s
    ntip_x = cx + LAYOUT["nose_tip_dx"] * s
    ntip_y = cy + LAYOUT["nose_tip_dy"] * s

    # Straight dorsal bridge descending from left-center brow to tip, then turning into septum
    nose_bridge_and_tip = _chain_beziers(
        [
            (
                (ntop_x, ntop_y),
                (ntop_x - 1.5 * s, (ntop_y + ntip_y) * 0.5),
                (ntip_x, ntip_y - 2.0 * s),
            ),
            (
                (ntip_x, ntip_y - 2.0 * s),
                (ntip_x - 1.5 * s, ntip_y + 4.5 * s),
                (cx - 2.0 * s, ntip_y + 4.5 * s),
                (cx + 6.0 * s, ntip_y + 2.5 * s),
            ),
        ],
        steps=28,
    )
    _tapered_stroke(
        draw,
        nose_bridge_and_tip,
        w_start=4.8 * s,
        w_mid=6.2 * s,
        w_end=3.8 * s,
        fill=WHITE,
        peak_t=0.62,
    )

    # Right nostril wing (ala) in subtle 3/4 view
    nostril_wing = _quad_bezier(
        (cx + 5.0 * s, ntip_y + 2.5 * s),
        (cx + 15.0 * s, ntip_y + 2.0 * s),
        (cx + 12.0 * s, ntip_y - 6.5 * s),
        steps=20,
    )
    _tapered_stroke(draw, nostril_wing, w_start=3.6 * s, w_mid=4.4 * s, w_end=2.2 * s, fill=WHITE)

    # Subtle shadow plane edge along the right side of the nose bridge
    nose_side_plane = _quad_bezier(
        (cx + 4.0 * s, cy - 5.0 * s),
        (cx + 7.0 * s, cy + 5.0 * s),
        (cx + 9.0 * s, ntip_y - 7.0 * s),
        steps=20,
    )
    _tapered_stroke(draw, nose_side_plane, w_start=1.6 * s, w_mid=2.8 * s, w_end=1.4 * s, fill=WHITE)

    return (
        int(cx - tx - 8 * s),
        int(cy - 36 * s),
        int(cx + tx + 8 * s),
        int(neck_bot_y + 16 * s),
    )


def draw_hair(
    draw: ImageDraw.ImageDraw,
    cx: float = LAYOUT["cx"] * SCALE,
    cy: float = LAYOUT["cy"] * SCALE,
    s: float = float(SCALE),
) -> tuple[int, int, int, int]:
    """Draw Hermes's curly Greek locks peeking out under the petasos brim and temples."""

    def _draw_curl(
        ccx: float,
        ccy: float,
        rx: float,
        ry: float,
        start_deg: float,
        sweep_deg: float,
        w_outer: float = 5.6,
    ) -> None:
        # Solid black backing disc so each curl lock cleanly overlaps the temple/forehead
        pad = 3.5 * s
        draw.ellipse(
            [ccx - rx - pad, ccy - ry - pad, ccx + rx + pad, ccy + ry + pad],
            fill=BLACK,
        )
        # Spiral C-curve sampled smoothly
        steps = 28
        pts: list[Point] = []
        for i in range(steps + 1):
            t = i / steps
            ang = math.radians(start_deg + sweep_deg * t)
            # Spiral tightens slightly as t approaches 1.0
            shrink = 1.0 - 0.32 * t
            pts.append((ccx + math.cos(ang) * rx * shrink, ccy + math.sin(ang) * ry * shrink))
        _tapered_stroke(
            draw,
            pts,
            w_start=w_outer * 0.55 * s,
            w_mid=w_outer * s,
            w_end=1.8 * s,
            fill=WHITE,
            peak_t=0.4,
        )

    # 1. Temple curls cascading on the left and right sides under the brim
    temple_curls = [
        # Left temple locks (top to bottom)
        (-62.0, -30.0, 14.0, 12.0, 110.0, 230.0, 5.8),
        (-65.0, -14.0, 13.5, 11.5, 120.0, 225.0, 5.5),
        (-60.0, 2.0, 11.5, 10.5, 130.0, 215.0, 5.0),
        # Right temple locks (top to bottom)
        (62.0, -30.0, 14.0, 12.0, 70.0, -230.0, 5.8),
        (65.0, -14.0, 13.5, 11.5, 60.0, -225.0, 5.5),
        (60.0, 2.0, 11.5, 10.5, 50.0, -215.0, 5.0),
    ]
    for dx, dy, rx, ry, st_deg, sw_deg, w_out in temple_curls:
        _draw_curl(cx + dx * s, cy + dy * s, rx * s, ry * s, st_deg, sw_deg, w_out)

    # 2. Forehead curls peeking out directly beneath the petasos brim shadow
    forehead_curls = [
        (-42.0, -37.0, 11.5, 8.5, 20.0, 210.0, 5.0),
        (-21.0, -40.0, 11.5, 8.5, 15.0, 215.0, 5.2),
        (0.0, -41.0, 12.0, 8.5, 165.0, -215.0, 5.2),
        (21.0, -40.0, 11.5, 8.5, 160.0, -215.0, 5.2),
        (42.0, -37.0, 11.5, 8.5, 155.0, -210.0, 5.0),
    ]
    for dx, dy, rx, ry, st_deg, sw_deg, w_out in forehead_curls:
        _draw_curl(cx + dx * s, cy + dy * s, rx * s, ry * s, st_deg, sw_deg, w_out)

    return (
        int(cx - 82 * s),
        int(cy - 52 * s),
        int(cx + 82 * s),
        int(cy + 16 * s),
    )


def draw_eyes(
    draw: ImageDraw.ImageDraw,
    cx: float = LAYOUT["cx"] * SCALE,
    cy: float = LAYOUT["cy"] * SCALE,
    s: float = float(SCALE),
    eyes_open: bool = True,
) -> dict[str, tuple[int, int, int, int]]:
    """Draw Hermes's almond eyes (open with iris + catchlight, or closed for blink)."""
    ey = cy + LAYOUT["eye_dy"] * s
    erx = LAYOUT["eye_rx"] * s
    ery = LAYOUT["eye_ry"] * s
    boxes: dict[str, tuple[int, int, int, int]] = {}

    for sign, key, dx_key in (
        (-1, "eye_left", "eye_left_dx"),
        (1, "eye_right", "eye_right_dx"),
    ):
        ex = cx + LAYOUT[dx_key] * s
        boxes[key] = (
            int(ex - erx - 4 * s),
            int(ey - ery - 4 * s),
            int(ex + erx + 4 * s),
            int(ey + ery + 4 * s),
        )

        # Clear eye socket area first
        draw.ellipse(
            [ex - erx - 4 * s, ey - ery - 4 * s, ex + erx + 4 * s, ey + ery + 4 * s],
            fill=BLACK,
        )

        if eyes_open:
            # Almond sclera polygon (upper and lower Bezier arches)
            upper_arch = _cubic_bezier(
                (ex - erx, ey + 1.0 * s),
                (ex - erx * 0.55, ey - ery * 1.35),
                (ex + erx * 0.55, ey - ery * 1.35),
                (ex + erx, ey + 0.5 * s),
                steps=28,
            )
            lower_arch = _cubic_bezier(
                (ex + erx, ey + 0.5 * s),
                (ex + erx * 0.50, ey + ery * 1.25),
                (ex - erx * 0.50, ey + ery * 1.25),
                (ex - erx, ey + 1.0 * s),
                steps=28,
            )
            draw.polygon(upper_arch + lower_arch, fill=WHITE)

            # Bold upper eyelid lash/crease stroke above the almond eye
            lid_crest = _cubic_bezier(
                (ex - erx - 2.5 * s, ey + 0.5 * s),
                (ex - erx * 0.55, ey - ery * 1.65),
                (ex + erx * 0.55, ey - ery * 1.65),
                (ex + erx + 2.5 * s, ey - 0.5 * s),
                steps=28,
            )
            _tapered_stroke(
                draw, lid_crest, w_start=2.2 * s, w_mid=4.2 * s, w_end=2.6 * s, fill=WHITE
            )

            # Dark iris & pupil positioned for a calm, friendly gaze
            iris_r = 6.2 * s
            ix = ex - 1.0 * s
            iy = ey - 0.4 * s
            draw.ellipse([ix - iris_r, iy - iris_r, ix + iris_r, iy + iris_r], fill=BLACK)

            # Crisp white specular catchlight inside the upper-left of the iris
            glint_r = 2.4 * s
            gx = ix - 2.2 * s
            gy = iy - 2.0 * s
            draw.ellipse([gx - glint_r, gy - glint_r, gx + glint_r, gy + glint_r], fill=WHITE)
        else:
            # Closed eye: bold downward-curving brush stroke + outer lash flick
            closed_curve = _cubic_bezier(
                (ex - erx - 2.0 * s, ey - 1.0 * s),
                (ex - erx * 0.45, ey + ery * 1.15),
                (ex + erx * 0.45, ey + ery * 1.15),
                (ex + erx + 3.0 * s, ey - 1.5 * s),
                steps=28,
            )
            _tapered_stroke(
                draw,
                closed_curve,
                w_start=3.2 * s,
                w_mid=6.2 * s,
                w_end=3.6 * s,
                fill=WHITE,
                peak_t=0.5,
            )
            # Subtle upper eyelid fold line above closed lid
            fold_curve = _quad_bezier(
                (ex - erx * 0.75, ey - 4.5 * s),
                (ex, ey - 1.5 * s),
                (ex + erx * 0.75, ey - 4.5 * s),
                steps=20,
            )
            _tapered_stroke(
                draw, fold_curve, w_start=1.4 * s, w_mid=2.6 * s, w_end=1.4 * s, fill=WHITE
            )
            _ = sign

    return boxes


def draw_mouth(
    draw: ImageDraw.ImageDraw,
    cx: float = LAYOUT["cx"] * SCALE,
    cy: float = LAYOUT["cy"] * SCALE,
    s: float = float(SCALE),
    mouth: str | tuple[str, float, float] = "smile",
) -> tuple[int, int, int, int]:
    """Draw Hermes's calm confident smile or progressive open-mouth speaking shapes."""
    mx = cx + LAYOUT["mouth_dx"] * s
    my = cy + LAYOUT["mouth_dy"] * s
    mrx = LAYOUT["mouth_rx"] * s

    if mouth == "smile":
        # Confident, friendly upward-curving comic smile
        smile_pts = _cubic_bezier(
            (mx - mrx, my - 3.5 * s),
            (mx - mrx * 0.42, my + 6.5 * s),
            (mx + mrx * 0.42, my + 6.0 * s),
            (mx + mrx + 1.5 * s, my - 4.5 * s),
            steps=32,
        )
        _tapered_stroke(
            draw,
            smile_pts,
            w_start=3.0 * s,
            w_mid=5.8 * s,
            w_end=3.2 * s,
            fill=WHITE,
            peak_t=0.5,
        )

        # Corner dimple/smile-crease accents at left and right corners
        for side, corner_x, corner_y in (
            (-1, mx - mrx - 1.0 * s, my - 3.5 * s),
            (1, mx + mrx + 2.5 * s, my - 4.5 * s),
        ):
            crease = _quad_bezier(
                (corner_x - side * 2.0 * s, corner_y - 4.5 * s),
                (corner_x + side * 2.5 * s, corner_y - 0.5 * s),
                (corner_x - side * 1.0 * s, corner_y + 4.0 * s),
                steps=16,
            )
            _tapered_stroke(draw, crease, w_start=1.6 * s, w_mid=3.6 * s, w_end=1.6 * s, fill=WHITE)

        # Lower-lip shadow hollow + sculptural lower-lip highlight stroke
        lower_lip = _quad_bezier(
            (mx - 10.0 * s, my + 11.5 * s),
            (mx, my + 14.5 * s),
            (mx + 11.0 * s, my + 11.0 * s),
            steps=20,
        )
        _tapered_stroke(draw, lower_lip, w_start=1.8 * s, w_mid=4.0 * s, w_end=1.8 * s, fill=WHITE)

        return (
            int(mx - mrx - 6 * s),
            int(my - 10 * s),
            int(mx + mrx + 8 * s),
            int(my + 18 * s),
        )

    # Speaking mouth: ("open", half_h_1x, half_w_1x)
    _, half_h_1x, half_w_1x = mouth
    hh = half_h_1x * s
    hw = half_w_1x * s

    top_y = my - hh * 0.35
    bot_y = my + hh * 1.35

    upper_lip = _cubic_bezier(
        (mx - hw, my - 1.5 * s),
        (mx - hw * 0.45, top_y - 3.0 * s),
        (mx + hw * 0.45, top_y - 3.0 * s),
        (mx + hw, my - 2.0 * s),
        steps=28,
    )
    lower_lip = _cubic_bezier(
        (mx + hw, my - 2.0 * s),
        (mx + hw * 0.65, bot_y + 3.5 * s),
        (mx - hw * 0.65, bot_y + 3.5 * s),
        (mx - hw, my - 1.5 * s),
        steps=28,
    )

    # 1. Dark oral cavity interior
    draw.polygon(upper_lip + lower_lip, fill=BLACK)

    # 2. Upper teeth band gleaming inside the top of the open mouth
    teeth_bot_y = min(bot_y - 2.5 * s, top_y + max(3.5 * s, hh * 0.42))
    teeth_poly = _quad_bezier(
        (mx - hw * 0.78, my - 1.0 * s),
        (mx, top_y - 1.0 * s),
        (mx + hw * 0.78, my - 1.2 * s),
        steps=20,
    ) + _quad_bezier(
        (mx + hw * 0.78, teeth_bot_y),
        (mx, teeth_bot_y + 1.5 * s),
        (mx - hw * 0.78, teeth_bot_y),
        steps=20,
    )
    draw.polygon(teeth_poly, fill=WHITE)

    # 3. Bold white upper and lower lip contours
    _tapered_stroke(
        draw, upper_lip, w_start=3.2 * s, w_mid=5.2 * s, w_end=3.2 * s, fill=WHITE
    )
    _tapered_stroke(
        draw, lower_lip, w_start=3.2 * s, w_mid=5.6 * s, w_end=3.2 * s, fill=WHITE
    )

    return (
        int(mx - hw - 6 * s),
        int(top_y - 8 * s),
        int(mx + hw + 6 * s),
        int(bot_y + 10 * s),
    )


# --------------------------------------------------------------------------- #
# Frame Assembly & 4x-to-1x Threshold Downsampling
# --------------------------------------------------------------------------- #
def _talk_spec(index: int) -> tuple[float, float, float]:
    """Return ``(half_h_1x, half_w_1x, jaw_drop_1x)`` for progressive ``talk_1..talk_4``."""
    # Progressively wider and more open across talk_1 -> talk_2 -> talk_3 -> talk_4
    specs = [
        (5.5, 17.5, 2.0),   # talk_1: slightly parted lips
        (10.5, 20.5, 5.0),  # talk_2: moderately open mouth
        (16.0, 23.5, 8.5),  # talk_3: wide open mouth
        (22.0, 26.5, 12.5), # talk_4: fullest open speaking mouth
    ]
    if not 1 <= index <= len(specs):
        raise ValueError(f"talk index out of range (1..{len(specs)}): {index}")
    return specs[index - 1]


def _scale_box(box: tuple[int, int, int, int], inv_scale: float) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    return (
        int(round(x0 * inv_scale)),
        int(round(y0 * inv_scale)),
        int(round(x1 * inv_scale)),
        int(round(y1 * inv_scale)),
    )


def _draw(
    size: int,
    *,
    eyes_open: bool = True,
    mouth: str | tuple[str, float, float] = "smile",
    wing_shift: float = 0.0,
    jaw_drop: float = 0.0,
) -> tuple[Image.Image, dict[str, tuple[int, int, int, int]]]:
    """Render one frame at 4x resolution in 'L' and downsample with threshold to mode '1'."""
    hi_size = size * SCALE
    s = hi_size / float(SIZE)
    cx = LAYOUT["cx"] * s
    cy = LAYOUT["cy"] * s

    hi_img = Image.new("L", (hi_size, hi_size), BLACK)
    d = ImageDraw.Draw(hi_img)

    # Layer order: wings -> helmet dome/brim -> face/neck/brows/nose -> curly hair -> eyes -> mouth
    draw_wing(d, cx=cx, cy=cy, s=s, side=-1, wing_shift=wing_shift)
    draw_wing(d, cx=cx, cy=cy, s=s, side=1, wing_shift=wing_shift)
    draw_helmet(d, cx=cx, cy=cy, s=s)
    draw_face(d, cx=cx, cy=cy, s=s, jaw_drop=jaw_drop)
    draw_hair(d, cx=cx, cy=cy, s=s)
    eye_boxes_hi = draw_eyes(d, cx=cx, cy=cy, s=s, eyes_open=eyes_open)
    mouth_box_hi = draw_mouth(d, cx=cx, cy=cy, s=s, mouth=mouth)

    resample = getattr(Image, "Resampling", Image).LANCZOS
    downsampled = hi_img.resize((size, size), resample=resample)
    img_1bit = downsampled.point(lambda p: 255 if p >= THRESHOLD else 0, mode="1")

    inv_scale = 1.0 / SCALE
    boxes = {
        "eye_left": _scale_box(eye_boxes_hi["eye_left"], inv_scale),
        "eye_right": _scale_box(eye_boxes_hi["eye_right"], inv_scale),
        "mouth": _scale_box(mouth_box_hi, inv_scale),
    }
    return img_1bit, boxes


def _normalize_frame_name(name: str) -> str:
    """Normalize shorthand names like 'idle1' or 'talk_2' to canonical '.png' filenames."""
    clean = name.strip()
    if not clean.endswith(".png"):
        clean = f"{clean}.png"
    aliases = {
        "idle1.png": "idle_1.png",
        "idle2.png": "idle_2.png",
        "talk1.png": "talk_1.png",
        "talk2.png": "talk_2.png",
        "talk3.png": "talk_3.png",
        "talk4.png": "talk_4.png",
    }
    return aliases.get(clean, clean)


def build_frame(
    name: str, size: int = SIZE
) -> tuple[Image.Image, dict[str, tuple[int, int, int, int]]]:
    """Build a single Hermes frame by filename. Returns ``(image, boxes)``."""
    canonical = _normalize_frame_name(name)
    if canonical in ("idle_1.png", "idle_2.png"):
        return _draw(
            size,
            eyes_open=True,
            mouth="smile",
            wing_shift=0.0 if canonical == "idle_1.png" else 1.0,
            jaw_drop=0.0,
        )
    if canonical == "blink.png":
        return _draw(
            size,
            eyes_open=False,
            mouth="smile",
            wing_shift=0.0,
            jaw_drop=0.0,
        )
    if canonical.startswith("talk_") and canonical.endswith(".png"):
        index = int(canonical[len("talk_") : -len(".png")])
        half_h, half_w, jaw_drop = _talk_spec(index)
        return _draw(
            size,
            eyes_open=True,
            mouth=("open", half_h, half_w),
            wing_shift=0.0,
            jaw_drop=jaw_drop,
        )
    raise ValueError(f"unknown frame name: {name!r}")


def feature_boxes(name: str, size: int = SIZE) -> dict[str, tuple[int, int, int, int]]:
    """Bounding boxes for the drawn features of ``name`` (for the geometry check)."""
    return build_frame(name, size)[1]


# --------------------------------------------------------------------------- #
# Frame Comparison
# --------------------------------------------------------------------------- #
def pixel_diff(a: Image.Image, b: Image.Image) -> int:
    """Number of pixels that differ between two same-sized images."""
    la = a.convert("L")
    lb = b.convert("L")
    if la.size != lb.size:
        raise ValueError(f"size mismatch: {la.size} vs {lb.size}")
    diff = ImageChops.difference(la, lb)
    colors = diff.getcolors(maxcolors=la.width * la.height + 1)
    assert colors is not None
    return sum(cnt for cnt, col in colors if col)


# --------------------------------------------------------------------------- #
# Manifest & Review Artifacts
# --------------------------------------------------------------------------- #
def build_manifest(
    frames_spec: Sequence[tuple[str, str, int]] = FRAMES,
    size: int = SIZE,
    diffs: dict[str, int] | None = None,
) -> dict:
    """Assemble the manifest dict (does not write it)."""
    manifest: dict = {
        "generator": "facesmith.hermes_face",
        "format": "1bit",
        "mode": "1",
        "size": [size, size],
        "background": "black",
        "foreground": "white",
        "frames": [
            {"file": name, "role": role, "duration_ms": dur}
            for name, role, dur in frames_spec
        ],
        "roles": {},
        "diff_threshold_px": MIN_DIFF_PX,
    }
    for role in sorted({r for _, r, _ in frames_spec}):
        manifest["roles"][role] = [n for n, r, _ in frames_spec if r == role]
    if diffs is not None:
        manifest["diffs"] = diffs
    return manifest


def build_contact_sheet(
    items: Iterable[tuple[str, Image.Image]], size: int = SIZE, cols: int = 4
) -> Image.Image:
    """A white RGB sheet laying out every frame with a label, for human review."""
    _ = size
    item_list = list(items)
    cell = 160
    pad = 12
    label_h = 24
    title_h = 34
    n = len(item_list)
    rows = max(1, ceil(n / cols))
    w = cols * cell + (cols + 1) * pad
    h = rows * (cell + label_h) + (rows + 1) * pad + title_h
    sheet = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    d.text(
        (pad, pad),
        "facesmith contact sheet - 1-bit Hermes frames",
        fill=(0, 0, 0),
    )

    resample = getattr(Image, "Resampling", Image).NEAREST
    for i, (name, img) in enumerate(item_list):
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad)
        y = title_h + pad + r * (cell + pad + label_h)
        small = img.convert("L").resize((cell, cell), resample).convert("RGB")
        sheet.paste(small, (x, y))
        d.text(
            (x, y + cell + 4),
            f"{name}  [{ROLES.get(name, '?')}]",
            fill=(0, 0, 0),
        )
    return sheet


def build_geometry_check(
    img: Image.Image,
    boxes: dict[str, tuple[int, int, int, int]],
    size: int = SIZE,
) -> Image.Image:
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
# Orchestration & CLI
# --------------------------------------------------------------------------- #
def _frame_images(size: int = SIZE):
    images = {}
    boxes = {}
    for name, _, _ in FRAMES:
        img, bx = build_frame(name, size)
        images[name] = img
        boxes[name] = bx
    return images, boxes


def _compute_diffs(images: dict[str, Image.Image]) -> dict[str, int]:
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
    """Generate every frame + manifest + review artifacts into ``out_dir``."""
    os.makedirs(out_dir, exist_ok=True)

    images, boxes = _frame_images(size)

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m facesmith.hermes_face",
        description="Generate 1-bit Hermes smartwatch face frames and review artifacts.",
    )
    parser.add_argument("out_dir", help="Output directory for generated frames and manifest.")
    parser.add_argument("--size", type=int, default=SIZE, help=f"Canvas size in px (default: {SIZE}).")
    args = parser.parse_args(argv)
    generate(args.out_dir, size=args.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
