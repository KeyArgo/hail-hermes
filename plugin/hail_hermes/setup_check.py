"""Plain-language environment checks for the first-run wizard.

Each check returns ``{"id", "state", "ok", "message", "fix"}`` with wording for
non-technical users. ``state`` is one of:

* ``"ok"`` - the check ran and passed.
* ``"action"`` - the check ran and found something the user should fix.
* ``"unknown"`` - the check could not run (missing dependency, no hub, no
  device) and so must never be reported as ok.

A check that blows up reports "Could not check this." with state ``"unknown"``
instead of raising, so the wizard always renders.
"""

from __future__ import annotations

import shutil
import sys
from typing import Any, Callable

_UNCHECKED_MESSAGE = "Could not check this."
_STATE_OK = "ok"
_STATE_ACTION = "action"
_STATE_UNKNOWN = "unknown"
_STATES = (_STATE_OK, _STATE_ACTION, _STATE_UNKNOWN)


def _entry(check_id: str, state: str, message: str, fix: str = "") -> dict[str, Any]:
    """Build one check row; ``ok`` is true only for a check that really ran and passed."""
    return {
        "id": check_id,
        "state": state,
        "ok": state == _STATE_OK,
        "message": message,
        "fix": fix,
    }


def _unknown(check_id: str, reason: str) -> dict[str, Any]:
    return _entry(check_id, _STATE_UNKNOWN, reason)


def _check_ffmpeg() -> dict[str, Any]:
    if shutil.which("ffmpeg"):
        return _entry("ffmpeg", _STATE_OK, "ffmpeg is installed.")
    return _entry(
        "ffmpeg",
        _STATE_ACTION,
        "Voice replies need a small free tool called ffmpeg.",
        "Install ffmpeg, then press Check again.",
    )


def _check_hermes() -> dict[str, Any]:
    if shutil.which("hermes"):
        return _entry("hermes", _STATE_OK, "Hermes was found.")
    return _entry(
        "hermes",
        _STATE_ACTION,
        "Hermes was not found on this computer.",
        "Install Hermes, then press Check again.",
    )


def _check_python() -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        return _entry("python", _STATE_OK, "This Python is new enough.")
    return _entry(
        "python",
        _STATE_ACTION,
        "Hail Hermes needs Python 3.11 or newer.",
        "Install Python 3.11 or newer, then press Check again.",
    )


def _check_voice() -> dict[str, Any]:
    try:
        from .hermes_voice import hermes_available
    except ImportError:
        return _unknown("voice", "Voice support isn't installed on this computer.")

    if hermes_available():
        return _entry("voice", _STATE_OK, "Hermes can listen and speak.")

    # hermes_available() imports Hermes' speech modules, and that import fails for reasons that have
    # nothing to do with speech — a missing third-party module, or a caller whose sys.path does not
    # include Hermes' own dependencies. Reporting "can't speak" then tells the owner the product is
    # broken while the hub is speaking fine (observed: it said so while audio was being produced).
    # Only claim speech is missing when the import works and the callables are absent.
    try:
        import tools.tts_tool  # noqa: F401
        import tools.transcription_tools  # noqa: F401
    except ModuleNotFoundError as exc:
        return _unknown(
            "voice",
            f"Couldn't check speech from here ({exc.name} is missing in this process). "
            "Press Talk in the pane to confirm it works.",
        )
    except Exception:
        return _unknown("voice", "Couldn't check speech from here. Press Talk in the pane to confirm.")

    return _entry(
        "voice",
        _STATE_ACTION,
        "Hermes can't listen or speak yet. You can still type.",
        "Set up speech and a voice in Hermes Settings.",
    )


def _check_speech_input() -> dict[str, Any]:
    """Say out loud when speech input is a test stub.

    A stub returns the same sentence for every utterance, so the product appears to listen while
    nothing is transcribed. That is a fake feature, and the owner's rule is to have none.
    """
    try:
        from .settings import read_settings

        cfg = read_settings()
    except Exception:
        return _unknown("speech", "Couldn't read the audio settings from here.")
    audio = cfg.get("audio") if isinstance(cfg.get("audio"), dict) else {}
    stt = audio.get("stt") if isinstance(audio.get("stt"), dict) else {}
    backend = str(stt.get("backend") or "auto").strip().lower()
    if backend == "stub":
        return _entry(
            "speech",
            _STATE_ACTION,
            "Speech input is a test stub: every utterance is read back as the same sentence.",
            "Set audio.stt.backend to auto or hermes, then restart the hub.",
        )
    return _entry("speech", _STATE_OK, f"Speech input uses {backend}.")


_CHECKS: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
    ("ffmpeg", _check_ffmpeg),
    ("hermes", _check_hermes),
    ("python", _check_python),
    ("voice", _check_voice),
    ("speech", _check_speech_input),
)


def _normalise(check_id: str, raw: Any) -> dict[str, Any]:
    """Accept only a well-formed row; anything else counts as a check that could not run."""
    if not isinstance(raw, dict):
        return _unknown(check_id, _UNCHECKED_MESSAGE)
    state = raw.get("state")
    if state not in _STATES:
        return _unknown(check_id, _UNCHECKED_MESSAGE)
    return _entry(
        check_id,
        state,
        str(raw.get("message", "")),
        str(raw.get("fix", "")),
    )


def run_checks() -> list[dict[str, Any]]:
    """Run every setup check; never raises, returns one entry per check."""
    results: list[dict[str, Any]] = []
    for check_id, check in _CHECKS:
        try:
            results.append(_normalise(check_id, check()))
        except Exception:
            results.append(_unknown(check_id, _UNCHECKED_MESSAGE))
    return results


def run_setup_check() -> dict[str, Any]:
    """The ``/setup-check`` payload: ``ok`` only when every check is really ok."""
    checks = run_checks()
    return {"ok": all(check["state"] == _STATE_OK for check in checks), "checks": checks}
