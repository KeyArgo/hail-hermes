"""Command-line entry point: ``python -m facesmith --out DIR``.

Generates the 1-bit mascot frames, manifest.json, contact_sheet.png and
geometry_check.png into the output directory.
"""

from __future__ import annotations

import argparse
import sys

from .core import FRAMES, SIZE, generate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="facesmith",
        description="Procedural 1-bit mascot frame generator for Hermes Pocket.",
    )
    parser.add_argument(
        "--out",
        default="mascot",
        help="output directory for frames, manifest and review artifacts (default: ./mascot)",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=SIZE,
        help=f"canvas edge in px, square canvas (default: {SIZE})",
    )
    parser.add_argument("--no-contact", action="store_true", help="skip contact_sheet.png")
    parser.add_argument("--no-geometry", action="store_true", help="skip geometry_check.png")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="do not assert the per-transition pixel-diff threshold",
    )
    args = parser.parse_args(argv)

    manifest = generate(
        args.out,
        args.size,
        write_contact=not args.no_contact,
        write_geometry=not args.no_geometry,
        verify=not args.no_verify,
    )

    n_frames = len(manifest["frames"])
    diffs = manifest.get("diffs", {})
    worst = min(diffs.values()) if diffs else None
    print(f"facesmith: wrote {n_frames} frames to {args.out!r} (size {args.size}x{args.size})")
    print(f"facesmith: manifest.json, contact_sheet.png, geometry_check.png")
    if worst is not None:
        print(f"facesmith: min transition diff = {worst}px (threshold {manifest['diff_threshold_px']}px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
