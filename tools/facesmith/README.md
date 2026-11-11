# tools/facesmith — mascot frame generator

Procedural, dependency-light generator for the Hermes Pocket mascot: a set of
**1-bit** (`mode "1"`) PNG frames on a **466×466** canvas, with the face
centered in the **top two-thirds** (round watch). Built with **Pillow only**.

The mascot is an original cute robot head — round head, antenna, two round
eyes, a smile that opens into talk shapes, and a cheek blush for the idle
animation. It is deliberately *not* any existing Nous/Hermes mascot.

## What it produces

Run the CLI (writes into `--out`, default `./mascot`):

```bash
python -m facesmith --out ../wear/src/main/assets/mascot/
```

Into the output directory it writes:

| File | Purpose |
|---|---|
| `idle_1.png`, `idle_2.png` | idle state (two frames, a gentle bob + cheek blush) |
| `blink.png` | blink state (eyes closed) |
| `talk_1.png` … `talk_4.png` | speaking state (four mouth shapes) |
| `manifest.json` | frame list, roles, per-frame `duration_ms`, recorded diffs |
| `contact_sheet.png` | all frames laid out for human review (RGB) |
| `geometry_check.png` | `idle_1` with labeled eye/mouth bounding boxes (RGB) |

Every frame is `mode "1"`, 466×466, white background / black ink.

## Animation model

`manifest.json` groups frames by role (`idle`, `blink`, `talk`) and records the
pixel difference across every animation transition. The transitions the wear
client walks — idle loop, into/out of blink, idle→talk, and the talk cycle —
are defined in `core.TRANSITIONS`. **Every transition must differ by more than
`MIN_DIFF_PX` (50 px)** so a blink / mouth change reads on a round 466 px LCD.
`generate()` verifies this and raises if any edge falls below the threshold, so
a regression can't ship silently. The pytest suite asserts the same.

## Library API

```python
from facesmith import generate, build_frame, pixel_diff, SIZE, FRAMES

manifest = generate("mascot")                 # write everything
img, boxes = build_frame("blink.png")         # (Image["1"], feature boxes)
delta = pixel_diff(a_img, b_img)              # count of differing pixels
```

## Tests

```bash
pip install -r requirements.txt      # Pillow (runtime)
pip install pytest                   # test-only dependency
pytest tools/facesmith               # from the repo root
```

The suite covers: dimensions, `mode "1"`, per-transition frame-diff thresholds
(> 50 px), manifest completeness (roles + durations), the recorded diffs
matching recomputed pixel deltas, and the contact/geometry review artifacts.

## Design choices (documented per the brief)

- **Direct `mode "1"` rendering.** Frames are drawn straight into a 1-bit image
  with `ImageDraw` (ellipses, arcs, lines) — no supersampling — so output is
  fully deterministic and byte-stable across runs (asserted by a test).
- **Frame-diff as the legibility contract.** Rather than a subjective "looks
  different", legibility is a hard, tested number: > 50 differing px per
  transition. The blink changes ~thousands of px (whole-eye open↔closed); talk
  shapes step the open-mouth oval area so each consecutive pair clears the bar.
- **Geometry check by construction, not detection.** Eye/mouth bounding boxes
  come from the same drawing parameters used to render, so `geometry_check.png`
  exactly reflects what was drawn.
- **`conftest.py`** puts the package on `sys.path` so `pytest tools/facesmith`
  works from the repo root without an install step.
- **`--size`** is parameterized (default 466) so the generator can target other
  square LCDs; geometry scales from the head radius.
