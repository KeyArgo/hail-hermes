# Testing

_How to run every suite, what each one covers, and the exact results measured at this commit._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

Two ways in: run the script, or hand this file to an agent on that machine and let it follow the
steps. Both end with a `RESULT.txt` you can send back.

## One command (Linux / macOS)

```bash
bash scripts/test.sh
```

## One command (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-windows.ps1
```

It creates `.venv-win`, installs the declared dependencies plus pytest, finds every `tests`
directory in the repo, runs each one, and writes `RESULT.txt`. It touches nothing outside this
folder. Prerequisite: Python 3.12 from python.org, installed with **"Add python.exe to PATH"**
ticked.

## Suites

| Suite | Command | What it covers |
|---|---|---|
| Plugin | `python -m pytest plugin/tests -q` | Protocol, pairing, hub turns, tools, settings, speech backends, the pane bundle, version identity |
| Simulator | `python -m pytest simulator/tests -q` | Device/hub protocol behaviour with no hardware |
| Face generator | `python -m pytest tools/facesmith/tests -q` | Frame generation, transitions and geometry |
| Wear OS | `cd wear && ./gradlew :app:assembleDebug :app:testDebugUnitTest` | Kotlin codecs, connection policy, playback, transcript state, gestures |
| Phone | `cd android && ./gradlew :app:assembleDebug :app:testDebugUnitTest` | Kotlin codec tests (protocol v1; not the 1.0.0 watch path) |

The Python suites do not need a watch, a microphone or a model. The Kotlin suites need JDK 17 and
Android SDK 35.

## Recorded results

| Host | OS | Python | Commit | Suites | Exit |
|---|---|---|---|---|---|
| Linux workstation | Linux 7.1.2 x86_64 | 3.13.13 | `7e2b487` | `441 passed, 1 skipped` (plugin) · `6 passed` (simulator) · `30 passed` (facesmith) | **0** |
| Windows | — | — | — | **not yet measured** | — |

**Total: 477 passed, 1 skipped.** The badge at the top of the README uses the passed count.

The Kotlin/Gradle suites were **not run on the Linux workstation used for this record** because it
has no JDK compiler installed (`java` is present but `javac` is not). CI runs them in
`.github/workflows/wear.yml` and `.github/workflows/android.yml` with JDK 17 and the runner's
Android SDK. A local result for them is still wanted; the commands above are the same ones CI runs.

The earlier record (`ce48e3e`, `373 passed, 1 skipped` across plugin + simulator) was measured from
a *deleted* virtual environment, so it proved a fresh machine could install and execute the suites.
This run repeats that property: `scripts/test.sh` creates its virtual environment outside the repo.

## What CI runs

| Workflow | Trigger | Command |
|---|---|---|
| `wear.yml` | changes under `wear/**` | `./gradlew :app:assembleDebug :app:testDebugUnitTest` |
| `android.yml` | changes under `android/**` | the same command in `android/` |

Both workflows use `actions/setup-java@v4` (Temurin 17) and `gradle/actions/setup-gradle@v4`. They
deliberately do **not** use `android-actions/setup-android@v3`, which currently fails on a
preview-license prompt; the Ubuntu runner ships the Android SDK. There is no Python CI workflow in
this repo at 1.0.0, so the Python counts above are local measurements recorded here.

## What the scripts install, and why that matters

Both scripts make a virtual environment, find every `tests` directory, and install the dependencies
of the **project each suite belongs to** — the directory one level up — including its `test` extra.

That is not incidental. Hail has no manifest at the repo root: they live in `plugin/` and
`tools/facesmith/`, and the parser tests need `numpy`, which is declared only in `plugin`'s `test`
extra. A root-only install — what these scripts did in their first version — installs nothing, and
every suite then fails on a missing import. That reads exactly like a broken build and is not one.
If a suite ever errors on `ModuleNotFoundError`, check the install step before the code.

The virtual environment is created **outside the repo** (`${TMPDIR:-/tmp}/hail-hermes-venv-test`).
It used to live in `.venv-test/` inside the tree, which meant a deploy copied 92 MB of symlinks into
the installed plugin — and the next `scripts/deploy.sh` refused to update, because a `lib64` symlink
cannot be followed into an update. Keep build and test state out of the tree that gets installed.

## What a good run looks like

- every suite reports `N passed`
- exit code `0`
- `RESULT.txt` lists the commit, the OS and the per-suite counts

A failure is still a useful result — send it. A missing result scores the same as never testing.

## If you are an agent on that machine, do this

1. Confirm `python --version` is 3.10+; if not, stop and ask for Python to be installed.
2. Run `powershell -ExecutionPolicy Bypass -File .\scripts\test-windows.ps1`.
3. Read `RESULT.txt`. Do not paraphrase it — quote the `suites:` lines verbatim.
4. If a suite failed, capture the last 40 lines of that suite's output and the traceback.
5. Report: host name, OS, python version, commit, and one line per suite.

Do not install packages globally, do not modify source to make tests pass, and do not report a pass
you did not see.

## Why this matters

The scoring system reads a recorded suite result at a commit as proof the code runs. A Windows result
is the evidence that turns "Windows untested" into a measured fact.

---

Back to [docs/README.md](docs/README.md).
