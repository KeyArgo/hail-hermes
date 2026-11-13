# Development

_Repo layout, how to run each suite, how CI works, and the rules that keep the build honest._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

## Repo layout

| Path | What | Built by |
|---|---|---|
| `protocol/PROTOCOL.md` | **The contract.** Every component implements this. | human |
| `plugin/` | Hermes hub plugin: WS server, pairing, agent bridge, device tools | Claude Code |
| `desktop/plugin.js` | **The desktop pane the app loads.** Byte-identical to the module below — see the note. | Hermes |
| `plugin/hail_hermes/desktop/plugin.js` | The pane's source, beside the Python package (pip layout and tests expect it there) | Hermes |
| `android/` | Phone hub client (Kotlin/Compose): voice, TTS, notifications, share-sheet | Codex |
| `wear/` | WearOS watch app: push-to-talk, mascot face states | Gemini CLI |
| `simulator/` | Device/hub simulator + pytest suite — test with zero hardware | Hermes/beam |
| `tools/facesmith/` | Mascot frame generator (1-bit idle/blink/talk) + contact sheet | Hermes/ornith |

The root `__init__.py`, `plugin.yaml`, `desktop/plugin.js` and `dashboard/` exist so a plain
`hermes plugins install` of the source repo is loadable. The Python package keeps the `plugin/`
pip layout because that is what the tests import.

## Run the tests

```bash
bash scripts/test.sh
```

The script creates a virtual environment outside the repo (`${TMPDIR:-/tmp}/hail-hermes-venv-test`),
installs the dependencies of the project each `tests` directory belongs to, runs every suite, and
writes `RESULT.txt`. On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-windows.ps1
```

A single suite:

```bash
python -m pytest plugin/tests -q
python -m pytest simulator/tests -q
python -m pytest tools/facesmith/tests -q
```

The Kotlin suites use the Gradle wrapper, which is **not tracked**: a prebuilt `gradle-wrapper.jar` is
a compiled binary nobody can review, so it is generated instead. Opening either project in Android
Studio does it, or one command with Gradle on PATH:

```bash
cd wear   && gradle wrapper --gradle-version 8.10.2 --distribution-type bin
cd android && gradle wrapper --gradle-version 8.10.2 --distribution-type bin
```

Then, as usual:

```bash
cd wear && ./gradlew :app:assembleDebug :app:testDebugUnitTest
cd android && ./gradlew :app:assembleDebug :app:testDebugUnitTest
```

`gradle/wrapper/gradle-wrapper.properties` is tracked and pins `distributionSha256Sum`, so the wrapper
validates the Gradle distribution it downloads. They need JDK 17 and Android SDK 35. The CI workflows use the runner's Android SDK rather than
`android-actions/setup-android`, which currently stops on a preview-license prompt.

## What CI runs

| Workflow | Trigger | Command |
|---|---|---|
| `.github/workflows/wear.yml` | changes under `wear/` or the workflow | `gradle wrapper` then `./gradlew :app:assembleDebug :app:testDebugUnitTest` |
| `.github/workflows/android.yml` | changes under `android/` or the workflow | the same two commands in `android/` |

The Python suites are run by `scripts/test.sh` and recorded in `TESTING.md`; there is no Python CI
workflow in this repo at 1.0.0. The badge at the top of the README points at `wear.yml`.

## Harnesses

| Harness | What it proves |
|---|---|
| `simulator/` | Device and hub behaviour over the real protocol, with no hardware. |
| `tools/pane_jsdom/` | The real `plugin.js` renders and every tab responds in jsdom. |
| `tools/pane_stub/` | The panel can be opened in a browser with a labelled stand-in SDK. |
| `tools/pane_e2e/` | Playwright against a real Hermes host (needs Hermes and Playwright installed). |
| `tools/audit_dashboard.py`, `tools/audit_watch.py` | Click every control and report what happened. |
| `tools/validate-repo.sh` | A `git archive` of HEAD validates as an installable plugin. |

## Versioning and build identity

- `plugin.yaml` names the release target; `plugin/plugin.yaml`, `plugin/pyproject.toml` and
  `hail_hermes.__version__` must agree. `plugin/tests/test_version_consistency.py` fails when the
  manifests disagree.
- `version_stamp.py` resolves `BUILD_INFO.json` → live git → `plugin.yaml`, and `GET /v1/version`
  reports version, commit, commit date, install time, dirty state and channel.
- `BUILD_INFO.json` is gitignored: a committed stamp goes stale silently.
- The pane holds no version constant; its header pill reads the hub, so it cannot advertise a build
  the backend is not running.

## Rules that are enforced by tests

1. `protocol/PROTOCOL.md` is law. Changes go through a PR against it, never silently.
2. Each component is self-contained; cross-component deps are the protocol only.
3. Never turn `desktop/plugin.js` into a re-export. The desktop loader accepts only
   `@hermes/plugin-sdk` and `react`, so a shim is a hard load failure.
   `plugin/tests/test_desktop_bundle.py` enforces the import surface and the byte-identical copy.
4. Test virtualenvs stay outside the tree; an installed plugin must not carry a venv.
5. A pairing shortcut that approves a device without the owner is a build failure
   (`plugin/tests/test_pairing.py`).

## Release checklist

1. Update `plugin.yaml`, `plugin/plugin.yaml`, `plugin/pyproject.toml`, `hail_hermes.__version__`
   and `CHANGELOG.md` together.
2. Run `bash scripts/test.sh` and record the exact summary in `TESTING.md`.
3. Run `bash tools/validate-repo.sh`; it must print `OK: a git clone of this repo is installable`.
4. Build the APK with the Gradle command above and attach it to the release.
5. Tag the commit; do not commit a `BUILD_INFO.json`.

---

Back to [docs/README.md](README.md).
