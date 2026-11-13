"""The environment a child process gets — chosen explicitly, never inherited wholesale.

Review item 2. Building a child's environment from ``dict(os.environ)`` hands every credential in the
parent process to a program this plugin did not write: a TTS/STT worker, ffmpeg, a user-configured
``command`` backend, and ``adb``. On a machine running Hermes those variables commonly include provider
API keys and tokens, so the child inherits secrets for no functional reason.

What a child legitimately needs is small: enough to find its own binaries and libraries, to know where
"home" and "temp" are, and the locale. Everything else is dropped unless it is one of the plugin's own
``POCKET_TTS_*`` knobs.

The list is an allowlist, not a denylist: a denylist has to guess every name a secret might use, while
an allowlist fails closed when someone adds a new one.
"""

from __future__ import annotations

import os
from typing import Mapping

# Names a normal child needs. Windows entries are included unconditionally because they are simply
# absent elsewhere; `SYSTEMROOT` in particular is required for socket/registry calls on Windows.
KEEP = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_RUNTIME_DIR",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "PULSE_SERVER",
    "PULSE_RUNTIME_PATH",
)

# The plugin's own knobs: a voice engine reads POCKET_TTS_DEVICE / _THREADS / _VOICE, and the state
# directory tells a worker where to write. Nothing here is a credential.
KEEP_PREFIXES = ("POCKET_TTS_", "POCKET_STATE_DIR")


def minimal_env(extra: Mapping[str, str] | None = None, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment a child process should receive.

    ``source`` exists so a test can hand in a synthetic parent environment; production callers leave it
    at the real one. ``extra`` is applied last, for the few values a specific child needs (a venv's
    ``VIRTUAL_ENV``, a pinned library path).
    """
    src = os.environ if source is None else source
    env = {name: src[name] for name in KEEP if name in src}
    for name, value in src.items():
        if name.startswith(KEEP_PREFIXES):
            env[name] = value
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env
