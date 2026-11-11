"""facesmith — procedural 1-bit mascot frame generator (Hermes Pocket)."""

from __future__ import annotations

from .core import (
    FRAMES,
    MIN_DIFF_PX,
    ROLES,
    SIZE,
    TRANSITIONS,
    build_contact_sheet,
    build_frame,
    build_geometry_check,
    build_manifest,
    feature_boxes,
    generate,
    pixel_diff,
)

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

__version__ = "0.1.0"
