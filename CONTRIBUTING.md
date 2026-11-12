# Contributing to Hail Hermes

_How to set up the repo, run the suites, and send a change that can be reviewed._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

## Before you start

- Read [protocol/PROTOCOL.md](protocol/PROTOCOL.md). It is the contract every client implements.
  Changes to it go in their own PR with the client changes, never silently inside another change.
- Read [docs/PLUGIN-DESIGN.md](docs/PLUGIN-DESIGN.md) for the decisions behind the current design.
- Keep the existing voice: short, plain, concrete sentences. No marketing claims.

## Set up

```bash
git clone https://github.com/KeyArgo/hail-hermes
cd hail-hermes
python3 -m venv .venv
.venv/bin/pip install -e "plugin[test]"
```

You do not need a watch, a microphone or a model to work on the hub or the pane. The simulator and
the fixture agent cover the protocol.

The pane is plain ESM: `desktop/plugin.js` and `plugin/hail_hermes/desktop/plugin.js` must stay
byte-identical. The desktop loader accepts only `@hermes/plugin-sdk` and `react` imports, so do not
add a re-export shim; `plugin/tests/test_desktop_bundle.py` enforces both rules.

## Run the tests

```bash
bash scripts/test.sh
```

Or one suite at a time:

```bash
.venv/bin/python -m pytest plugin/tests -q
.venv/bin/python -m pytest simulator/tests -q
.venv/bin/python -m pytest tools/facesmith/tests -q
```

The watch and phone suites:

```bash
cd wear && ./gradlew :app:assembleDebug :app:testDebugUnitTest
cd android && ./gradlew :app:assembleDebug :app:testDebugUnitTest
```

These need JDK 17 and Android SDK 35. See [TESTING.md](TESTING.md) for what each suite covers and
the recorded results.

## Commit style

- One logical change per commit.
- Prefix with the area: `docs:`, `ci:`, `fix:`, `test:`, `feat:`, `chore:`.
- Say what changed and why in the body when the subject is not enough.
- Do not commit `BUILD_INFO.json`, a virtual environment, build outputs, or secrets.

## Pull requests

1. Fork or branch from `main`.
2. Make the change and run the suites it touches.
3. Open a PR using the template: summary, tests run, checklist.
4. If a test fails, say so and include the traceback. A known failure is better than a claimed
   pass.
5. Keep protocol changes separate and link the client changes that implement them.

## Reporting bugs and asking questions

- Bugs and feature requests: open an issue with the template.
- Questions and show-and-tell: use [Discussions](https://github.com/KeyArgo/hail-hermes/discussions).
- Security problems: do **not** open a public issue. Use GitHub's private vulnerability reporting,
  as described in [SECURITY.md](SECURITY.md).

## What not to send

- Credentials, tokens, `.env` files or personal paths in screenshots.
- A test that passes only because it skipped the thing it was meant to test.
- Product claims that the code does not support.
