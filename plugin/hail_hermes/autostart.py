"""Start a local Hail Hermes hub when the Desktop pane needs one."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from .protocol import DEFAULT_PORT

from . import filelock
from .control import ControlClient
from .store import Store, atomic_write


class HubStartError(RuntimeError):
    """The hub did not answer its control channel during startup."""


def _popen_detach_kwargs() -> dict[str, int]:
    """Per-platform kwargs so spawned hubs survive their parent (POSIX session / Windows group)."""
    if os.name == "nt":
        # No console window: a user closing one would kill the hub.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _answers(state_dir: Path) -> bool:
    """Check the live snapshot and authenticate to its control channel."""
    try:
        client = ControlClient(state_dir, timeout=0.25)
        if not client.live():
            return False
        client.call("devices")
        return True
    except Exception:
        return False


def _last_log_line(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        return next((line.strip() for line in reversed(lines) if line.strip()), "")
    except OSError:
        return ""


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless probe on Windows: it raises
        # OSError WinError 87 for a pid the caller does not own. Ask the kernel.
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _hub_argv(
    directory: Path,
    plugin_dir: Path | None = None,
    hermes_cli: str | bool | None = None,
) -> list[str]:
    """Build the command that starts a hub.

    Prefer the ``hermes`` launcher: the hub imports Hermes' own speech tooling
    (``tools.tts_tool``, ``hermes_cli``), and only the launcher puts those on
    sys.path. Started any other way voice replies fail with ``tts_failed``.

    Fallbacks, for a Hermes that has no launcher on PATH: a pip-installed
    package runs as ``python -m hail_hermes``; Hermes itself loads plugins from
    a hyphenated directory (``hail-hermes``), where that module name does not
    exist, so load this file's own package by path instead.
    """
    # Honour the LAN setting: restarting used to come back loopback-only, so the watch lost
    # the computer and every restart looked like a failure of the restart.
    try:
        from .settings import read_settings

        _lan = bool((read_settings(directory) or {}).get("lan_enabled"))
    except Exception:
        _lan = False
    _bind = ["--lan"] if _lan else ["--host", "127.0.0.1"]
    args = ["--state-dir", str(directory), "serve", *_bind]
    cli = shutil.which("hermes") if hermes_cli is None else hermes_cli
    if cli:
        return [str(cli), "hail", *args]
    plugin_dir = Path(plugin_dir) if plugin_dir is not None else Path(__file__).resolve().parent
    if plugin_dir.name == "hail_hermes":
        # Source or pip-installed layout: the module name exists as-is.
        return [sys.executable, "-m", "hail_hermes", *args]
    bootstrap = "\n".join([
        "import importlib.util, sys",
        "plugin, directory, lan = sys.argv[1], sys.argv[2], sys.argv[3] == '1'",
        "spec = importlib.util.spec_from_file_location(",
        "    'hail_hermes', plugin + '/__init__.py', submodule_search_locations=[plugin])",
        "module = importlib.util.module_from_spec(spec)",
        "sys.modules['hail_hermes'] = module",
        "spec.loader.exec_module(module)",
        "sys.argv = ['hail_hermes', '--state-dir', directory, 'serve'] + (['--lan'] if lan else ['--host', '127.0.0.1'])",
        "from hail_hermes.cli import main",
        "raise SystemExit(main())",
    ])
    return [sys.executable, "-c", bootstrap, str(plugin_dir), str(directory), "1" if _lan else "0"]


def ensure_hub(
    state_dir: str | Path | None,
    *,
    command: Sequence[str] | None = None,
    timeout: float = 30.0,
) -> bool:
    """Ensure a hub answers and return whether this call started it.

    The optional command is intended for tests. No process is terminated here:
    if an earlier auto-started process is still alive but unresponsive, retrying
    reports a failure rather than starting another process alongside it.
    """
    store = Store(state_dir)
    directory = store.dir
    lock_path = directory / "autostart.lock"
    pid_path = directory / "autostart.pid"
    log_path = directory / "hub.log"

    with lock_path.open("a+") as lock:
        filelock.lock(lock)
        try:
            if _answers(directory):
                return False

            # An unresponsive process recorded by an earlier attempt may still
            # finish starting. Do not launch a second hub while it is alive.
            try:
                previous_pid = int(pid_path.read_text().strip())
            except (OSError, ValueError):
                previous_pid = 0

            if previous_pid and _process_exists(previous_pid):
                raise HubStartError(
                    _last_log_line(log_path)
                    or "The previously started Hail Hermes hub is not responding."
                )

            argv = list(command) if command is not None else _hub_argv(directory)
            env = os.environ.copy()
            env["HERMES_POCKET_AUTOSTART"] = "1"
            try:
                with log_path.open("ab") as log:
                    process = subprocess.Popen(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        **_popen_detach_kwargs(),
                        env=env,
                    )
                atomic_write(pid_path, f"{process.pid}\n")
            except OSError as exc:
                raise HubStartError(
                    _last_log_line(log_path) or str(exc)
                ) from exc

            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                if _answers(directory):
                    return True
                if process.poll() is not None or time.monotonic() >= deadline:
                    break
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

            raise HubStartError(
                _last_log_line(log_path)
                or "Hail Hermes hub did not answer its control channel."
            )
        finally:
            filelock.unlock(lock)


def _listener_pid(port: int) -> int | None:
    """Whoever is listening on the hub port, whatever started it."""
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and "LISTENING" in line:
                    return int(parts[-1])
        else:
            out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=20).stdout
            for line in out.splitlines():
                if f":{port} " in line and "pid=" in line:
                    return int(line.split("pid=")[1].split(",")[0])
    except Exception:
        return None
    return None


def restart_hub(state_dir: str | Path | None, *, timeout: float = 30.0) -> dict[str, object]:
    """Stop the hub on the current machine and start a fresh one.

    This exists because "how do I restart it?" was a real question twice, and the honest
    answer was a netstat/taskkill dance. Returns what actually happened so a caller can
    print it instead of guessing.
    """
    import subprocess
    import sys
    import time as _time

    directory = Store(state_dir).dir
    pid_path = directory / "autostart.pid"
    stopped = False
    pid: int | None = None
    try:
        pid = int(pid_path.read_text().strip())
    except Exception:
        pid = None
    if pid is None or not _process_exists(pid):
        # A hub started from a shortcut or from `hermes hail serve` never writes autostart.pid,
        # which is why `hermes hail restart` used to report stopped=false and change nothing.
        pid = _listener_pid(DEFAULT_PORT)
    if pid and _process_exists(pid):
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=20)
            else:
                os.kill(pid, 15)
            stopped = True
        except Exception:
            stopped = False
    deadline = _time.monotonic() + 15.0
    while _time.monotonic() < deadline and _answers(directory):
        _time.sleep(0.3)
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass
    started = ensure_hub(state_dir, timeout=timeout)
    return {"stopped": stopped, "pid": pid, "started": started, "answers": _answers(directory)}
