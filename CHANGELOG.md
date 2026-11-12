# Changelog — Hail Hermes

Versions follow the suite's `VERSIONING-STANDARD.md`: `plugin.yaml` names the release target, and
each build carries its real identity (version · commit · date · dirty) from `version_stamp.py`,
shown **in the pane header** and at `GET /v1/version`. The status bar carries only the ⌚ chip.

## Unreleased

- **A catalog install no longer reads `-dev`.** The version chip treated any untagged checkout as a development build, and a catalog install carries no tags.
- **The pane stub tool registers the plugin the way the test harness does.**
- **The version chip tells a release from a development build.** A catalog install of the pinned commit, or a checkout at the release tag, reads `X.Y.Z`; any other build reads `X.Y.Z-dev`. `tools/release_check.py` and the release workflow keep the version, the changelog, the tag and the GitHub release in step.
- **Documentation:** a rebuilt README with a recorded demo and current Hermes Desktop screenshots, and a full docs set (usage, reference, architecture, configuration, troubleshooting, FAQ, development and testing).
- **Project files:** contributing guide, code of conduct, security policy, issue and pull request templates, and CODEOWNERS.

## 1.0.0 — 2026-11-11

First public release.

- **Hold to talk, hear the answer.** Replies are spoken, and the face moves only while audio is really playing.
- **A 60-second demo in the panel:** three real questions answered aloud through the real hub, no watch needed.
- **Pair a watch in four digits.** It finds this computer on the same Wi-Fi and reconnects by itself after the hub restarts or the network drops.
- **The agent starts the conversation.** Scheduled nudges reach you on the panel or your wrist.
- **Any model, including local ones.** Hermes subscription, LM Studio, Ollama or vLLM, switched live; the panel header says which one is answering.
- **Device tools for the agent** while a watch is connected: notify, say, haptic and battery.
- **Weather and day briefs** answered in seconds without a model round trip.

## Development history (before 1.0.0)

The notes below were written during development, before the first public release.

### A destination in the app
- **A left-rail entry.** `SIDEBAR_NAV_AREA` gives Hail Hermes a place in Hermes Desktop's own sidebar,
  and `ROUTES_AREA` serves `/hail` as a full page. The status-bar ⌚ chip and the shortcut stay as the
  quick path; the rail is what makes it a destination rather than a popup.
- **One version pill.** The pane used to draw two different version strings — a monospace
  `v… · date · sha` in the header and `v1.1.0 · build …` in Settings. There is now a single pill in the
  interface font with a status dot; date, commit and channel live in its tooltip. The pane holds no
  version constant at all, so it can only ever show the build the hub is running.

### The panel is the product's face
- **Talk opens the panel** and carries the drawn watch as its hero; the live mirror sits beneath the
  conversation instead of above it.
- **Four tabs — Talk · Sessions · Character · Settings.** Watch is no longer a peer tab: it lives
  behind **More**, and a watch that is asking to pair raises a banner on Talk with its code and
  Approve / Deny, so nobody without hardware opens a hardware tab first.
- **A header pill says what the panel is doing** — `Ready · Quick ask`, or `Hub not running` — and
  opens Settings.
- **Honest first-run states**: "No model yet. Pick a local one or add a Hermes key." (only when both
  the configured sources and Hermes' own model list are empty), "Still waiting — check the hub."
  after ten seconds, and the existing hub-unreachable row with Retry.

### Models are findable and ordered
- **The Brain list is searchable**: a filter box over a clickable list, matching provider and model
  name, with an honest empty state and a `(saved)` row when the live list has not caught up.
- **Watch presets are priorities 1–5**: numbered rows with ↑ / ↓ to reorder and ✕ to remove, because
  the wrist cycles them in that order.
- The list is the union of `/models` (local endpoints) and `/hermes-models` (Hermes providers); it
  used to be the latter only, so a machine serving models locally saw an empty list.

### The build states its identity in one place
- The build stamp moved from a second status-bar item into the **pane header** (and the Settings
  line). `PANE_VERSION` / `PANE_BUILD` remain as fallbacks; the chip itself reads `/v1/version`, so a
  stale constant cannot advertise a build the backend is not running.
- **One release number**: `1.0.0` in `plugin.yaml`, `plugin/plugin.yaml`, the package manifest,
  `pyproject.toml`, `__version__` and `PANE_VERSION`. The parallel `0.2.0` release line was
  withdrawn — a number below the `v1.0.0` tag cannot be the next release.
- `test_the_three_manifests_tell_one_story` fails when the three manifests disagree, and
  `test_the_pane_carries_the_build_stamp_and_the_status_bar_does_not` pins the single-stamp decision.

### Reliability and the repo itself
- **`desktop/plugin.js` must be the real module**, byte-identical to
  `plugin/hail_hermes/desktop/plugin.js`; a re-export shim is a hard load failure because the desktop
  loader accepts only `@hermes/plugin-sdk` and `react` (README rule 6, enforced by
  `plugin/tests/test_desktop_bundle.py`).
- **Simulator tests take an ephemeral port** instead of hard-coding 8799, so a parallel run or a
  leftover dev server no longer fails all six with `EADDRINUSE`.
- **Test virtualenvs live outside the repo** (`${TMPDIR:-/tmp}/hail-hermes-venv-test`): one inside the
  tree was copied into the installed plugin and blocked the next deploy with "links are not followed
  into an update".
- Swallow-and-continue sites in the brief and TTS-cache paths log at debug instead of passing silently.

### Earlier in 1.0.0

- **The build states its own identity.** `hail_hermes/version_stamp.py` (vendored from the suite
  template) resolves `BUILD_INFO.json` → live `git` → `plugin.yaml`; `GET /v1/version` reports
  version, commit, commit date, install time, dirty state and channel.
- A `HailVersionChip` in the status bar reads that route and shows
  `v1.0.0-dev.N · 2026-11-09 · 19f7a2c`, amber with `modified` when the installed copy came from a
  dirty tree, and copies the line on click for a bug report.
- `BUILD_INFO.json` is gitignored: a committed stamp goes stale silently.
- **Fixed the version chip's colour token.** It read `text.tertiary`, which the module never
  declares (`textPrimary` / `textSecondary` only), so the chip threw `ReferenceError` on render —
  the one affordance that must always work. It now uses `textSecondary`, and the jsdom pane harness
  renders the chip for real, so the regression cannot come back unnoticed.
- One release target: `plugin.yaml`, `plugin/plugin.yaml`, `plugin/hail_hermes/plugin.yaml`,
  `plugin/pyproject.toml` and `hail_hermes.__version__` all say `1.0.0`, asserted by a test.
- `edge-tts` is declared as the optional extra `tts`, next to its guarded lazy import.
- Removed dead wrist-pane state (`matchCode`, `micRef`) and a `/soul` fetch whose result was
  discarded on the Character tab; `hub.py` no longer imports two names it never used.
- Dead imports removed: `os` in `cli.py`, the two voice ids in `plugin_api.py`.
  `scripts/verify-served.py` is executable now that it has a shebang.
