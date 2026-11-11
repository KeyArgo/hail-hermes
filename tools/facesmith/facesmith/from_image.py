"""Build a watch-face character frame set from a black-and-white reference portrait using Pillow."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

CANVAS_SIZE = 466
UPPER_HEIGHT = int(round(CANVAS_SIZE * 2 / 3))  # 311 px (upper 2/3 of 466x466 canvas)

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


class FeatureBox:
    """Detected bounding box and center in canvas coordinates."""

    def __init__(self, cx: int, cy: int, w: int, h: int) -> None:
        self.cx = cx
        self.cy = cy
        self.w = w
        self.h = h

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        hw = max(4, self.w // 2)
        hh = max(3, self.h // 2)
        return (self.cx - hw, self.cy - hh, self.cx + hw, self.cy + hh)


def prepare_portrait(input_img: Image.Image) -> Tuple[Image.Image, Image.Image, Tuple[int, int, int, int]]:
    """
    Crop/fit the input portrait into the upper 2/3 of a 466x466 black canvas,
    and invert so dark line art becomes light on a pure black background.

    Returns:
        base_rgb: 466x466 RGB image (inverted line art on black canvas)
        portrait_gray: 466x466 L image of original dark-on-light values (255 outside portrait)
        portrait_rect: (x0, y0, x1, y1) of the fitted portrait region on the canvas
    """
    if input_img.mode in ("RGBA", "LA") or (input_img.mode == "P" and "transparency" in input_img.info):
        rgba = input_img.convert("RGBA")
        white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        gray_src = Image.alpha_composite(white_bg, rgba).convert("L")
    else:
        gray_src = input_img.convert("L")

    # Trim uniform outer whitespace so the face fills the upper 2/3 nicely
    inv_for_trim = ImageOps.invert(gray_src).point(lambda p: 255 if p > 24 else 0)
    content_bbox = inv_for_trim.getbbox()
    if content_bbox:
        bw = content_bbox[2] - content_bbox[0]
        bh = content_bbox[3] - content_bbox[1]
        if bw >= 32 and bh >= 32:
            pad_x = int(bw * 0.06)
            pad_y = int(bh * 0.06)
            crop_box = (
                max(0, content_bbox[0] - pad_x),
                max(0, content_bbox[1] - pad_y),
                min(gray_src.width, content_bbox[2] + pad_x),
                min(gray_src.height, content_bbox[3] + pad_y),
            )
            gray_src = gray_src.crop(crop_box)

    # Fit into the upper 2/3 area (max width 360, max height UPPER_HEIGHT - 16)
    target_w = 340
    target_h = UPPER_HEIGHT - 16
    fitted_gray = ImageOps.contain(gray_src, (target_w, target_h), method=Image.Resampling.LANCZOS)
    fitted_inv = ImageOps.invert(fitted_gray)

    # Soft vignette fade near portrait edges so the pasted box blends into the black OLED canvas
    px_inv = fitted_inv.load()
    fw, fh = fitted_inv.size
    margin = max(6, min(fw, fh) // 14)
    for y in range(fh):
        fy = min(1.0, y / margin, (fh - 1 - y) / margin)
        for x in range(fw):
            fx = min(1.0, x / margin, (fw - 1 - x) / margin)
            fade = min(fx, fy)
            val = int(px_inv[x, y] * fade)
            # Clean near-black background noise to pure OLED black
            px_inv[x, y] = 0 if val < 18 else val

    paste_x = (CANVAS_SIZE - fw) // 2
    paste_y = max(8, (UPPER_HEIGHT - fh) // 2)
    portrait_rect = (paste_x, paste_y, paste_x + fw, paste_y + fh)

    canvas_inv = Image.new("L", (CANVAS_SIZE, CANVAS_SIZE), 0)
    canvas_inv.paste(fitted_inv, (paste_x, paste_y))

    canvas_gray = Image.new("L", (CANVAS_SIZE, CANVAS_SIZE), 255)
    canvas_gray.paste(fitted_gray, (paste_x, paste_y))

    return canvas_inv.convert("RGB"), canvas_gray, portrait_rect


def _find_darkest_patch(
    gray_img: Image.Image,
    x_min: int,
    x_max: int,
    y_min: int,
    y_max: int,
    win_w: int,
    win_h: int,
) -> Optional[Tuple[FeatureBox, float]]:
    """
    Search [x_min..x_max, y_min..y_max] for the window with maximum mean darkness (255 - gray).
    Returns (FeatureBox, mean_darkness) or None if the search region is invalid.
    """
    win_w = max(8, win_w)
    win_h = max(6, win_h)
    if x_max - x_min <= win_w or y_max - y_min <= win_h:
        return None

    px = gray_img.load()
    step = max(2, min(win_w, win_h) // 4)
    best_score = -1.0
    best_cx = (x_min + x_max) // 2
    best_cy = (y_min + y_max) // 2

    hw = win_w // 2
    hh = win_h // 2

    for cy in range(y_min + hh, y_max - hh + 1, step):
        for cx in range(x_min + hw, x_max - hw + 1, step):
            total = 0
            count = 0
            for y in range(cy - hh, cy + hh, 2):
                for x in range(cx - hw, cx + hw, 2):
                    total += 255 - px[x, y]
                    count += 1
            if count == 0:
                continue
            score = total / count
            if score > best_score:
                best_score = score
                best_cx = cx
                best_cy = cy

    return FeatureBox(best_cx, best_cy, win_w, win_h), best_score


def detect_eyes_and_mouth(
    portrait_gray: Image.Image,
    portrait_rect: Tuple[int, int, int, int],
) -> Optional[Tuple[FeatureBox, FeatureBox, FeatureBox]]:
    """
    Detect the two darkest eye regions in the upper-middle band and the mouth region below them.
    Returns (left_eye, right_eye, mouth) or None if detection fails validation checks.
    """
    x0, y0, x1, y1 = portrait_rect
    pw = x1 - x0
    ph = y1 - y0
    if pw < 48 or ph < 48:
        return None

    # Check overall contrast so blank/flat images fail cleanly
    crop_pixels = list(portrait_gray.crop(portrait_rect).getdata())
    min_val = min(crop_pixels)
    max_val = max(crop_pixels)
    if (max_val - min_val) < 35:
        return None

    # Upper-middle band for the two eyes
    band_y0 = y0 + int(ph * 0.25)
    band_y1 = y0 + int(ph * 0.65)
    mid_x = x0 + pw // 2
    margin_x = int(pw * 0.14)
    center_gap = max(6, int(pw * 0.04))

    eye_w = max(16, int(pw * 0.16))
    eye_h = max(12, int(ph * 0.10))

    left_res = _find_darkest_patch(
        portrait_gray,
        x0 + margin_x,
        mid_x - center_gap,
        band_y0,
        band_y1,
        eye_w,
        eye_h,
    )
    right_res = _find_darkest_patch(
        portrait_gray,
        mid_x + center_gap,
        x1 - margin_x,
        band_y0,
        band_y1,
        eye_w,
        eye_h,
    )
    if left_res is None or right_res is None:
        return None

    left_eye, left_score = left_res
    right_eye, right_score = right_res

    # Validate eye darkness and horizontal/vertical symmetry
    min_eye_darkness = 18.0
    if left_score < min_eye_darkness or right_score < min_eye_darkness:
        return None

    eye_dx = right_eye.cx - left_eye.cx
    eye_dy = abs(right_eye.cy - left_eye.cy)
    if eye_dx < int(pw * 0.16) or eye_dy > int(ph * 0.18):
        return None

    # Search for the mouth below the eyes, horizontally centered between them
    eyes_cy = (left_eye.cy + right_eye.cy) // 2
    eyes_mid_x = (left_eye.cx + right_eye.cx) // 2
    mouth_y0 = min(y1 - 12, eyes_cy + max(10, int(eye_dx * 0.30)))
    mouth_y1 = min(y1 - 6, eyes_cy + max(24, int(eye_dx * 0.95)))
    mouth_x0 = max(x0 + margin_x, eyes_mid_x - int(eye_dx * 0.36))
    mouth_x1 = min(x1 - margin_x, eyes_mid_x + int(eye_dx * 0.36))

    mouth_w = max(14, int(eye_dx * 0.38))
    mouth_h = max(8, int(ph * 0.06))

    mouth_res = _find_darkest_patch(
        portrait_gray,
        mouth_x0,
        mouth_x1,
        mouth_y0,
        mouth_y1,
        mouth_w,
        mouth_h,
    )
    if mouth_res is not None and mouth_res[1] >= 8.0:
        mouth_box = mouth_res[0]
    else:
        # Geometric fallback below the eyes when the mouth is drawn very lightly
        default_mouth_y = min(y1 - 12, eyes_cy + int(eye_dx * 0.55))
        if default_mouth_y <= eyes_cy + 6:
            return None
        mouth_box = FeatureBox(eyes_mid_x, default_mouth_y, mouth_w, mouth_h)

    return left_eye, right_eye, mouth_box


def paint_blink_frame(
    base_rgb: Image.Image,
    left_eye: FeatureBox,
    right_eye: FeatureBox,
) -> Image.Image:
    """Paint the two detected eye regions as closed curved lids."""
    frame = base_rgb.copy()
    draw = ImageDraw.Draw(frame)

    for eye in (left_eye, right_eye):
        x0, y0, x1, y1 = eye.bbox
        # Mask out the open eye region in OLED black
        pad_x = max(3, eye.w // 6)
        pad_y = max(3, eye.h // 4)
        draw.ellipse((x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y), fill=(0, 0, 0))

        # Paint a soft curved closed eyelid arc in bright line-art white
        arc_box = (x0 - 2, eye.cy - eye.h // 2, x1 + 2, eye.cy + eye.h // 3)
        draw.arc(arc_box, start=18, end=162, fill=(238, 238, 242), width=3)

    return frame


def paint_talk_frame(
    base_rgb: Image.Image,
    mouth: FeatureBox,
    level: int,
) -> Image.Image:
    """Open the mouth as a small dark ellipse growing progressively per frame (level 1..4)."""
    frame = base_rgb.copy()
    draw = ImageDraw.Draw(frame)

    rx = max(4, int(mouth.w * (0.24 + 0.08 * level)))
    ry = max(2, int(mouth.h * (0.20 + 0.22 * level)))
    bbox = (mouth.cx - rx, mouth.cy - ry, mouth.cx + rx, mouth.cy + ry)

    # Light lip outline with a dark interior ellipse so it reads clearly on inverted line art
    draw.ellipse(
        bbox,
        fill=(12, 12, 14),
        outline=(232, 232, 238),
        width=2,
    )
    return frame


def paint_idle_2_frame(base_rgb: Image.Image) -> Image.Image:
    """Create a subtle head-bob frame for idle_2."""
    frame = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0))
    frame.paste(base_rgb, (0, -3))
    return frame


def paint_listening_frame(
    base_rgb: Image.Image,
    portrait_rect: Tuple[int, int, int, int],
) -> Image.Image:
    """Add small light sparkles near the upper side of the portrait for listening."""
    frame = base_rgb.copy()
    draw = ImageDraw.Draw(frame)
    _, y0, x1, _ = portrait_rect
    sx = min(CANVAS_SIZE - 28, x1 + 12)
    sy = max(28, y0 + 28)

    for dx, dy, r in [(0, 0, 7), (16, 18, 5), (-8, 28, 4)]:
        cx, cy = sx + dx, sy + dy
        draw.ellipse((cx - r, cy - 2, cx + r, cy + 2), fill=(200, 232, 255))
        draw.ellipse((cx - 2, cy - r, cx + 2, cy + r), fill=(200, 232, 255))
    return frame


def paint_thinking_frame(
    base_rgb: Image.Image,
    portrait_rect: Tuple[int, int, int, int],
) -> Image.Image:
    """Add a tiny dotted thought bubble in the upper-right for thinking."""
    frame = base_rgb.copy()
    draw = ImageDraw.Draw(frame)
    _, y0, x1, _ = portrait_rect
    bx = min(CANVAS_SIZE - 48, x1 + 8)
    by = max(36, y0 + 20)

    draw.ellipse((bx - 16, by + 22, bx - 8, by + 30), outline=(235, 235, 240), width=2)
    draw.ellipse((bx - 8, by + 8, bx + 4, by + 20), outline=(235, 235, 240), width=2)
    draw.ellipse((bx, by - 16, bx + 38, by + 10), fill=(16, 16, 20), outline=(235, 235, 240), width=2)
    for dx in (11, 19, 27):
        draw.ellipse((bx + dx - 2, by - 5, bx + dx + 2, by - 1), fill=(235, 235, 240))
    return frame


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
        draw.ellipse(
            (x0, y0, x0 + CANVAS_SIZE - 1, y0 + CANVAS_SIZE - 1),
            outline=(70, 70, 82),
            width=2,
        )
        draw.text((x0 + 8, y0 + CANVAS_SIZE + 6), name, fill=(230, 230, 240), font=font)

    return sheet


def generate_from_image(input_path: Path, outdir: Path) -> Dict[str, Image.Image]:
    """Load INPUT.png, build the 9-frame character set, and save frames + manifest + contact sheet."""
    with Image.open(input_path) as src:
        base_rgb, portrait_gray, portrait_rect = prepare_portrait(src)

    detected = detect_eyes_and_mouth(portrait_gray, portrait_rect)
    if detected is None:
        print(
            f"Warning: Eye/mouth detection failed for '{input_path.name}'; "
            "falling back to the static image for every frame.",
            file=sys.stdout,
        )
        frames = {name: base_rgb.copy() for name in FRAME_NAMES}
    else:
        left_eye, right_eye, mouth = detected
        frames = {
            "idle_1": base_rgb.copy(),
            "idle_2": paint_idle_2_frame(base_rgb),
            "blink": paint_blink_frame(base_rgb, left_eye, right_eye),
            "talk_1": paint_talk_frame(base_rgb, mouth, 1),
            "talk_2": paint_talk_frame(base_rgb, mouth, 2),
            "talk_3": paint_talk_frame(base_rgb, mouth, 3),
            "talk_4": paint_talk_frame(base_rgb, mouth, 4),
            "listening": paint_listening_frame(base_rgb, portrait_rect),
            "thinking": paint_thinking_frame(base_rgb, portrait_rect),
        }

    outdir.mkdir(parents=True, exist_ok=True)
    files_map: Dict[str, str] = {}
    for name, img in frames.items():
        filename = f"{name}.png"
        img.convert("RGB").save(outdir / filename, format="PNG")
        files_map[name] = filename

    manifest = {
        "name": input_path.stem,
        "source_image": input_path.name,
        "detection_succeeded": detected is not None,
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
    return frames


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a B&W portrait into an inverted watch-face frame set."
    )
    parser.add_argument("input", type=Path, help="Path to input portrait PNG.")
    parser.add_argument("outdir", type=Path, help="Output directory for generated PNGs and manifest.")
    args = parser.parse_args(argv)
    generate_from_image(args.input, args.outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
