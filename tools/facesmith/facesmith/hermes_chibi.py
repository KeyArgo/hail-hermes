"""Generate cute, friendly, colourful chibi Hermes watch-face frames using Pillow."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont

CANVAS_SIZE = 466
SCALE = 4
WORK_SIZE = CANVAS_SIZE * SCALE

FRAME_NAMES: List[str] = [
    "idle_1",
    "idle_2",
    "blink",
    "talk_1",
    "talk_2",
    "talk_3",
    "talk_4",
    "listening",
    "thinking",
]

# Easy-to-tweak 1x layout coordinates (fitted to upper 2/3 of 466x466 canvas: y in 0..310)
LAYOUT = {
    "cx": 233,
    "head_cy": 190,
    "head_rx": 124,
    "head_ry": 104,
    "ear_dx": 122,
    "ear_dy": 6,
    "ear_rx": 20,
    "ear_ry": 24,
    "eye_dx": 48,
    "eye_y": 192,
    "eye_rx": 27,
    "eye_ry": 31,
    "cheek_dx": 76,
    "cheek_y": 224,
    "cheek_rx": 22,
    "cheek_ry": 13,
    "mouth_y": 236,
    "mouth_w": 30,
    "mouth_h": 16,
    "helmet_cy": 106,
    "helmet_dome_rx": 112,
    "helmet_dome_ry": 68,
    "helmet_brim_y": 124,
    "helmet_brim_rx": 142,
    "helmet_brim_ry": 26,
    "wing_dx": 132,
    "wing_y": 96,
    "outline_width": 6,
}

# Warm, soft, colourful chibi palette
COLORS = {
    "bg": (0, 0, 0),
    "outline": (76, 44, 28),          # Thick soft dark-brown outline (not black)
    "skin": (255, 220, 192),          # Warm peachy skin tone
    "skin_shadow": (244, 196, 164),   # Subtle lower-face warmth
    "ear_inner": (242, 174, 152),
    "hair": (138, 82, 48),            # Soft brown curly hair
    "hair_highlight": (172, 110, 70),
    "cheek": (255, 148, 170),         # Rosy pink cheeks
    "cheek_highlight": (255, 198, 210),
    "eye_base": (58, 34, 24),         # Deep warm brown eye base
    "eye_iris": (186, 118, 64),       # Warm honey-amber lower iris glow
    "eye_upper_tint": (96, 62, 48),
    "white": (255, 255, 255),
    "mouth_inner": (206, 84, 102),
    "tongue": (255, 146, 162),
    "gold": (255, 214, 68),           # Shiny golden petasos helmet
    "gold_Steps": (236, 178, 38),
    "gold_brim": (248, 196, 52),
    "gold_shine": (255, 244, 176),
    "wing_white": (250, 252, 255),
    "wing_blue": (172, 222, 255),     # Light blue tint on inner wing edge
    "sparkle_blue": (118, 208, 255),
    "sparkle_core": (218, 246, 255),
    "thought_fill": (240, 248, 255),
}


def s(val: float) -> int:
    """Scale a 1x coordinate or dimension to the 4x working canvas."""
    return int(round(val * SCALE))


def _bbox(cx: float, cy: float, rx: float, ry: float) -> Tuple[int, int, int, int]:
    return (s(cx - rx), s(cy - ry), s(cx + rx), s(cy + ry))


def _draw_rounded_arc(
    draw: ImageDraw.ImageDraw,
    bbox: Tuple[int, int, int, int],
    start_deg: float,
    end_deg: float,
    color: Tuple[int, int, int],
    width_1x: float,
) -> None:
    """Draw an arc with rounded circular caps at both endpoints to avoid sharp angles."""
    w = s(width_1x)
    draw.arc(bbox, start=start_deg, end=end_deg, fill=color, width=w)

    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    rx = (x1 - x0) / 2.0 - w / 2.0
    ry = (y1 - y0) / 2.0 - w / 2.0
    cap_r = w / 2.0

    for deg in (start_deg, end_deg):
        rad = math.radians(deg)
        px = cx + rx * math.cos(rad)
        py = cy + ry * math.sin(rad)
        draw.ellipse((px - cap_r, py - cap_r, px + cap_r, py + cap_r), fill=color)


def draw_wing(
    draw: ImageDraw.ImageDraw,
    side: str,
    flutter: float = 0.0,
    y_offset: float = 0.0,
) -> None:
    """Draw a small fluffy white wing (4 rounded feathers with light-blue inner edge tint)."""
    sign = -1.0 if side == "left" else 1.0
    cx = LAYOUT["cx"] + sign * LAYOUT["wing_dx"]
    cy = LAYOUT["wing_y"] + y_offset - flutter * 6.0
    ow = LAYOUT["outline_width"]

    # 4 rounded feathers arranged from bottom/inner to top/outer
    feather_specs = [
        # (dx, dy, rx, ry)
        (sign * 8, 16 - flutter * 2, 22, 13),
        (sign * 18, 4 - flutter * 4, 28, 14),
        (sign * 26, -10 - flutter * 6, 32, 15),
        (sign * 16, -24 - flutter * 8, 26, 14),
    ]

    # Outer pass: soft dark-brown outlines for unified fluffy silhouette
    for dx, dy, rx, ry in feather_specs:
        fcx = cx + dx
        fcy = cy + dy
        draw.ellipse(_bbox(fcx, fcy, rx + ow * 0.85, ry + ow * 0.85), fill=COLORS["outline"])

    # Middle pass: fluffy white feathers + light blue inner edge tint
    for dx, dy, rx, ry in feather_specs:
        fcx = cx + dx
        fcy = cy + dy
        draw.ellipse(_bbox(fcx, fcy, rx, ry), fill=COLORS["wing_white"])
        # Light blue tint along the inner edge (closest to helmet)
        inner_cx = fcx - sign * (rx * 0.32)
        inner_cy = fcy + ry * 0.12
        draw.ellipse(
            _bbox(inner_cx, inner_cy, rx * 0.58, ry * 0.65),
            fill=COLORS["wing_blue"],
        )
        # Re-add soft white highlight on outer tip
        outer_cx = fcx + sign * (rx * 0.22)
        outer_cy = fcy - ry * 0.15
        draw.ellipse(
            _bbox(outer_cx, outer_cy, rx * 0.55, ry * 0.52),
            fill=COLORS["wing_white"],
        )

    # Subtle separator arcs between feathers
    for dx, dy, rx, ry in feather_specs[1:]:
        fcx = cx + dx
        fcy = cy + dy
        arc_box = _bbox(fcx, fcy, rx, ry)
        if side == "left":
            _draw_rounded_arc(draw, arc_box, 200, 315, COLORS["outline"], ow * 0.6)
        else:
            _draw_rounded_arc(draw, arc_box, 225, 340, COLORS["outline"], ow * 0.6)


def draw_head(draw: ImageDraw.ImageDraw, y_offset: float = 0.0) -> None:
    """Draw the big round chibi head and cute rounded ears with dark-brown outlines."""
    cx = LAYOUT["cx"]
    cy = LAYOUT["head_cy"] + y_offset
    rx = LAYOUT["head_rx"]
    ry = LAYOUT["head_ry"]
    ow = LAYOUT["outline_width"]

    # Rounded ears on left and right
    for sign in (-1, 1):
        ecx = cx + sign * LAYOUT["ear_dx"]
        ecy = cy + LAYOUT["ear_dy"]
        erx = LAYOUT["ear_rx"]
        ery = LAYOUT["ear_ry"]
        draw.ellipse(
            _bbox(ecx, ecy, erx, ery),
            fill=COLORS["skin"],
            outline=COLORS["outline"],
            width=s(ow),
        )
        draw.ellipse(
            _bbox(ecx - sign * 3, ecy + 2, erx * 0.5, ery * 0.5),
            fill=COLORS["ear_inner"],
        )

    # Main head base + warm lower-chin shadow + soft dark-brown outline
    draw.ellipse(_bbox(cx, cy, rx, ry), fill=COLORS["skin_shadow"])
    draw.ellipse(_bbox(cx, cy - 5, rx - 2, ry - 4), fill=COLORS["skin"])
    draw.ellipse(
        _bbox(cx, cy, rx, ry),
        fill=None,
        outline=COLORS["outline"],
        width=s(ow),
    )


def draw_hair(draw: ImageDraw.ImageDraw, y_offset: float = 0.0) -> None:
    """Draw soft brown curly hair peeking out from beneath the golden helmet."""
    cx = LAYOUT["cx"]
    cy = LAYOUT["head_cy"] + y_offset
    ow = LAYOUT["outline_width"]

    # Side curls near the temples and forehead curls peeking under the brim
    curl_specs = [
        # (dx, dy, rx, ry)
        (-102, -34, 24, 22),
        (-82, -52, 26, 22),
        (-50, -60, 28, 20),
        (-16, -63, 25, 19),
        (18, -62, 26, 20),
        (52, -59, 27, 20),
        (84, -52, 26, 22),
        (102, -34, 24, 22),
    ]

    # Outline pass
    for dx, dy, rx, ry in curl_specs:
        draw.ellipse(
            _bbox(cx + dx, cy + dy, rx + ow * 0.85, ry + ow * 0.85),
            fill=COLORS["outline"],
        )

    # Fill + soft highlight pass
    for dx, dy, rx, ry in curl_specs:
        draw.ellipse(_bbox(cx + dx, cy + dy, rx, ry), fill=COLORS["hair"])
        draw.ellipse(
            _bbox(cx + dx - 3, cy + dy - 3, rx * 0.55, ry * 0.48),
            fill=COLORS["hair_highlight"],
        )

    # Cute curl swirls along the bottom edge of the bangs
    for dx, dy, rx, ry in curl_specs[1:-1]:
        _draw_rounded_arc(
            draw,
            _bbox(cx + dx, cy + dy, rx, ry),
            25,
            155,
            COLORS["outline"],
            ow * 0.75,
        )


def draw_helmet(draw: ImageDraw.ImageDraw, y_offset: float = 0.0) -> None:
    """Draw the shiny golden petasos helmet with rounded dome, curved brim, and specular shine."""
    cx = LAYOUT["cx"]
    dome_cy = LAYOUT["helmet_cy"] + y_offset
    dome_rx = LAYOUT["helmet_dome_rx"]
    dome_ry = LAYOUT["helmet_dome_ry"]
    brim_cy = LAYOUT["helmet_brim_y"] + y_offset
    brim_rx = LAYOUT["helmet_brim_rx"]
    brim_ry = LAYOUT["helmet_brim_ry"]
    ow = LAYOUT["outline_width"]

    # Dome base + golden shading + shiny highlight
    draw.ellipse(
        _bbox(cx, dome_cy, dome_rx, dome_ry),
        fill=COLORS["gold_Steps"],
        outline=COLORS["outline"],
        width=s(ow),
    )
    draw.ellipse(
        _bbox(cx - 4, dome_cy - 4, dome_rx - 8, dome_ry - 8),
        fill=COLORS["gold"],
    )
    # Specular shine ovals on top-left of dome
    draw.ellipse(
        _bbox(cx - 38, dome_cy - 24, 34, 16),
        fill=COLORS["gold_shine"],
    )
    draw.ellipse(
        _bbox(cx + 8, dome_cy - 32, 12, 7),
        fill=COLORS["gold_shine"],
    )
    # Re-stroke dome outline cleanly
    draw.ellipse(
        _bbox(cx, dome_cy, dome_rx, dome_ry),
        fill=None,
        outline=COLORS["outline"],
        width=s(ow),
    )

    # Rounded petasos brim overlapping the lower dome
    draw.ellipse(
        _bbox(cx, brim_cy, brim_rx, brim_ry),
        fill=COLORS["gold_Steps"],
        outline=COLORS["outline"],
        width=s(ow),
    )
    draw.ellipse(
        _bbox(cx, brim_cy - 3, brim_rx - 6, brim_ry - 5),
        fill=COLORS["gold_brim"],
    )
    draw.ellipse(
        _bbox(cx - 28, brim_cy - 8, 64, 8),
        fill=COLORS["gold_shine"],
    )
    draw.ellipse(
        _bbox(cx, brim_cy, brim_rx, brim_ry),
        fill=None,
        outline=COLORS["outline"],
        width=s(ow),
    )


def draw_cheeks(
    draw: ImageDraw.ImageDraw,
    lift: float = 0.0,
    y_offset: float = 0.0,
) -> None:
    """Draw rosy pink cheeks that lift slightly when smiling or talking."""
    cx = LAYOUT["cx"]
    cy = LAYOUT["cheek_y"] + y_offset - lift * 6.0
    rx = LAYOUT["cheek_rx"]
    ry = LAYOUT["cheek_ry"]

    for sign in (-1, 1):
        ccx = cx + sign * LAYOUT["cheek_dx"]
        draw.ellipse(_bbox(ccx, cy, rx, ry), fill=COLORS["cheek"])
        draw.ellipse(
            _bbox(ccx - sign * 5, cy - 3, rx * 0.35, ry * 0.35),
            fill=COLORS["cheek_highlight"],
        )


def draw_eyes(
    draw: ImageDraw.ImageDraw,
    mode: str = "normal",
    y_offset: float = 0.0,
) -> None:
    """Draw huge sparkly chibi eyes with two white highlights each, or blink/listening/thinking variants."""
    cx = LAYOUT["cx"]
    cy = LAYOUT["eye_y"] + y_offset
    ow = LAYOUT["outline_width"]

    if mode == "blink":
        for sign in (-1, 1):
            ecx = cx + sign * LAYOUT["eye_dx"]
            # Happy curved closed eyelid arc with rounded caps
            _draw_rounded_arc(
                draw,
                _bbox(ecx, cy + 6, LAYOUT["eye_rx"], LAYOUT["eye_ry"] * 0.65),
                195,
                345,
                COLORS["outline"],
                ow * 1.15,
            )
            # Soft eyebrow above
            _draw_rounded_arc(
                draw,
                _bbox(ecx, cy - 18, 22, 10),
                200,
                340,
                COLORS["outline"],
                ow * 0.75,
            )
        return

    scale = 1.10 if mode == "listening" else 1.0
    look_dx = 7.0 if mode == "thinking" else 0.0
    look_dy = -7.0 if mode == "thinking" else 0.0

    rx = LAYOUT["eye_rx"] * scale
    ry = LAYOUT["eye_ry"] * scale

    for sign in (-1, 1):
        ecx = cx + sign * LAYOUT["eye_dx"]
        # Outer dark-brown eye base
        draw.ellipse(
            _bbox(ecx, cy, rx, ry),
            fill=COLORS["eye_base"],
            outline=COLORS["outline"],
            width=s(ow * 0.9),
        )

        # Warm amber lower-iris crescent
        iris_cx = ecx + look_dx * 0.6
        iris_cy = cy + ry * 0.22 + look_dy * 0.6
        draw.ellipse(
            _bbox(iris_cx, iris_cy, rx * 0.76, ry * 0.56),
            fill=COLORS["eye_iris"],
        )

        # Deep pupil center
        pupil_cx = ecx + look_dx * 0.7
        pupil_cy = cy - ry * 0.05 + look_dy * 0.7
        draw.ellipse(
            _bbox(pupil_cx, pupil_cy, rx * 0.62, ry * 0.55),
            fill=COLORS["eye_base"],
        )

        # Highlight 1: large sparkly white highlight (top-left)
        h1_cx = ecx - rx * 0.30 + look_dx
        h1_cy = cy - ry * 0.30 + look_dy
        draw.ellipse(
            _bbox(h1_cx, h1_cy, rx * 0.34, ry * 0.32),
            fill=COLORS["white"],
        )

        # Highlight 2: secondary white highlight (bottom-right)
        h2_cx = ecx + rx * 0.30 + look_dx * 0.8
        h2_cy = cy + ry * 0.28 + look_dy * 0.8
        draw.ellipse(
            _bbox(h2_cx, h2_cy, rx * 0.18, ry * 0.17),
            fill=COLORS["white"],
        )

        # Eyebrows (expressive per mode)
        brow_offset_y = -4.0 if mode == "listening" else (-5.0 if (mode == "thinking" and sign == 1) else 0.0)
        _draw_rounded_arc(
            draw,
            _bbox(ecx, cy - ry - 8 + brow_offset_y, 22, 9),
            200,
            340,
            COLORS["outline"],
            ow * 0.75,
        )


def draw_mouth(
    draw: ImageDraw.ImageDraw,
    open_amount: float = 0.0,
    y_offset: float = 0.0,
) -> None:
    """Draw a small happy smile when closed, or a progressively open rounded mouth when talking."""
    cx = LAYOUT["cx"]
    cy = LAYOUT["mouth_y"] + y_offset
    ow = LAYOUT["outline_width"]

    if open_amount <= 0.01:
        _draw_rounded_arc(
            draw,
            _bbox(cx, cy - 6, LAYOUT["mouth_w"] * 0.65, LAYOUT["mouth_h"] * 0.75),
            20,
            160,
            COLORS["outline"],
            ow * 0.9,
        )
        return

    # Progressively open rounded mouth
    rx = LAYOUT["mouth_w"] * (0.48 + 0.32 * open_amount)
    ry = LAYOUT["mouth_h"] * (0.35 + 0.85 * open_amount)
    mouth_cy = cy + ry * 0.25

    draw.ellipse(
        _bbox(cx, mouth_cy, rx, ry),
        fill=COLORS["mouth_inner"],
        outline=COLORS["outline"],
        width=s(ow * 0.85),
    )

    # Cute pink rounded tongue inside lower mouth
    tongue_rx = rx * 0.68
    tongue_ry = ry * 0.52
    tongue_cy = mouth_cy + ry * 0.32
    draw.ellipse(
        _bbox(cx, tongue_cy, tongue_rx, tongue_ry),
        fill=COLORS["tongue"],
    )

    # Re-stroke mouth outline so the tongue stays cleanly inside
    draw.ellipse(
        _bbox(cx, mouth_cy, rx, ry),
        fill=None,
        outline=COLORS["outline"],
        width=s(ow * 0.85),
    )


def _draw_listening_sparkles(draw: ImageDraw.ImageDraw, y_offset: float = 0.0) -> None:
    """Draw small rounded blue sparkles near the raised wing for the listening frame."""
    sparkles = [
        (388, 62 + y_offset, 9.0),
        (412, 96 + y_offset, 6.5),
        (372, 118 + y_offset, 5.5),
    ]
    for sx, sy, sr in sparkles:
        # Rounded cross-pills + glowing center circle (no sharp corners)
        draw.ellipse(_bbox(sx, sy, sr * 1.35, sr * 0.48), fill=COLORS["sparkle_blue"])
        draw.ellipse(_bbox(sx, sy, sr * 0.48, sr * 1.35), fill=COLORS["sparkle_blue"])
        draw.ellipse(_bbox(sx, sy, sr * 0.58, sr * 0.58), fill=COLORS["sparkle_core"])


def _draw_thinking_bubble(draw: ImageDraw.ImageDraw, y_offset: float = 0.0) -> None:
    """Draw a tiny dotted thought bubble in the upper-right for the thinking frame."""
    ow = LAYOUT["outline_width"] * 0.65
    # Trail of two small rising dots
    for bx, by, br in [(356, 98 + y_offset, 6.5), (374, 78 + y_offset, 9.5)]:
        draw.ellipse(
            _bbox(bx, by, br, br),
            fill=COLORS["thought_fill"],
            outline=COLORS["outline"],
            width=s(ow),
        )

    # Fluffy rounded thought cloud
    cloud_cx, cloud_cy = 404, 48 + y_offset
    puffs = [
        (-16, 4, 15, 13),
        (0, -4, 18, 15),
        (16, 4, 15, 13),
        (0, 8, 20, 12),
    ]
    for dx, dy, rx, ry in puffs:
        draw.ellipse(
            _bbox(cloud_cx + dx, cloud_cy + dy, rx + ow * 0.8, ry + ow * 0.8),
            fill=COLORS["outline"],
        )
    for dx, dy, rx, ry in puffs:
        draw.ellipse(
            _bbox(cloud_cx + dx, cloud_cy + dy, rx, ry),
            fill=COLORS["thought_fill"],
        )

    # Three tiny ellipsis dots inside the thought cloud
    for dx in (-11, 0, 11):
        draw.ellipse(
            _bbox(cloud_cx + dx, cloud_cy + 3, 2.8, 2.8),
            fill=COLORS["outline"],
        )


def render_frame(frame_name: str) -> Image.Image:
    """Render a single named chibi Hermes frame at 4x and downsample with LANCZOS to 466x466 RGB."""
    work_img = Image.new("RGB", (WORK_SIZE, WORK_SIZE), COLORS["bg"])
    draw = ImageDraw.Draw(work_img)

    # Per-frame parameters
    y_offset = 0.0
    flutter_left = 0.0
    flutter_right = 0.0
    eye_mode = "normal"
    open_amount = 0.0
    cheek_lift = 0.0

    if frame_name == "idle_2":
        y_offset = -4.0
        flutter_left = 1.0
        flutter_right = 1.0
    elif frame_name == "blink":
        eye_mode = "blink"
    elif frame_name == "talk_1":
        open_amount = 0.25
        cheek_lift = 0.25
    elif frame_name == "talk_2":
        open_amount = 0.50
        cheek_lift = 0.50
    elif frame_name == "talk_3":
        open_amount = 0.75
        cheek_lift = 0.75
    elif frame_name == "talk_4":
        open_amount = 1.00
        cheek_lift = 1.00
    elif frame_name == "listening":
        eye_mode = "listening"
        flutter_left = 0.2
        flutter_right = 1.8  # One wing raised
        cheek_lift = 0.3
    elif frame_name == "thinking":
        eye_mode = "thinking"

    # Layered character drawing
    draw_head(draw, y_offset=y_offset)
    draw_hair(draw, y_offset=y_offset)
    draw_wing(draw, "left", flutter=flutter_left, y_offset=y_offset)
    draw_wing(draw, "right", flutter=flutter_right, y_offset=y_offset)
    draw_helmet(draw, y_offset=y_offset)
    draw_cheeks(draw, lift=cheek_lift, y_offset=y_offset)
    draw_eyes(draw, mode=eye_mode, y_offset=y_offset)
    draw_mouth(draw, open_amount=open_amount, y_offset=y_offset)

    if frame_name == "listening":
        _draw_listening_sparkles(draw, y_offset=y_offset)
    elif frame_name == "thinking":
        _draw_thinking_bubble(draw, y_offset=y_offset)

    return work_img.resize((CANVAS_SIZE, CANVAS_SIZE), resample=Image.Resampling.LANCZOS)


def build_contact_sheet(frames: Dict[str, Image.Image], cols: int = 3) -> Image.Image:
    """Assemble all rendered frames into a labeled RGB contact sheet."""
    names = list(frames.keys())
    rows = math.ceil(len(names) / cols)
    cell_pad = 16
    label_h = 28
    cell_w = CANVAS_SIZE + cell_pad * 2
    cell_h = CANVAS_SIZE + cell_pad * 2 + label_h

    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    for idx, name in enumerate(names):
        r, c = divmod(idx, cols)
        x0 = c * cell_w + cell_pad
        y0 = r * cell_h + cell_pad
        sheet.paste(frames[name], (x0, y0))

        # Subtle round watch-bezel preview ring
        draw.ellipse(
            (x0, y0, x0 + CANVAS_SIZE - 1, y0 + CANVAS_SIZE - 1),
            outline=(70, 70, 82),
            width=2,
        )
        draw.text((x0 + 8, y0 + CANVAS_SIZE + 6), name, fill=(230, 230, 240), font=font)

    return sheet


def write_character_set(
    outdir: Path,
    character_name: str,
    frames: Dict[str, Image.Image],
) -> None:
    """Save all PNG frames, manifest.json, and contact_sheet.png to outdir."""
    outdir.mkdir(parents=True, exist_ok=True)
    files_map: Dict[str, str] = {}

    for name, img in frames.items():
        filename = f"{name}.png"
        img.convert("RGB").save(outdir / filename, format="PNG")
        files_map[name] = filename

    manifest = {
        "name": character_name,
        "canvas_size": [CANVAS_SIZE, CANVAS_SIZE],
        "mode": "RGB",
        "frames": list(frames.keys()),
        "files": files_map,
        "animations": {
            "idle": ["idle_1", "idle_2"],
            "blink": ["idle_1", "blink", "idle_1"],
            "talk": ["talk_1", "talk_2", "talk_3", "talk_4"],
            "listening": ["listening"],
            "thinking": ["thinking"],
        },
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    contact_sheet = build_contact_sheet(frames)
    contact_sheet.save(outdir / "contact_sheet.png", format="PNG")


def generate_all(outdir: Path) -> Dict[str, Image.Image]:
    """Render all chibi Hermes frames and write outputs to outdir."""
    frames = {name: render_frame(name) for name in FRAME_NAMES}
    write_character_set(outdir, "hermes_chibi", frames)
    return frames


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render cute chibi Hermes watch-face frames, manifest.json, and contact_sheet.png."
    )
    parser.add_argument("outdir", type=Path, help="Output directory for generated PNGs and manifest.")
    args = parser.parse_args(argv)
    generate_all(args.outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
