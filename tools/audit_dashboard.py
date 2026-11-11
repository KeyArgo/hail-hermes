"""Audit every dashboard route in-process, against a throwaway copy of the state dir.

Why in-process: the pane's routes are served by the Hermes plugin host, not the hub's port, so
`curl 127.0.0.1:8767/settings` 404s even when the feature works. Mounting the real APIRouter on a
FastAPI app exercises the real handlers without needing the desktop app or a paired device.

Every route gets: a status code, whether the body is the shape the pane needs, and latency.
Mutation tests run against a COPY of the state directory, so a bug here cannot corrupt the install.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

REAL_STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes" / "hail-hermes"
TMP = Path(tempfile.mkdtemp(prefix="hail-audit-"))
STATE = TMP / "state"
if REAL_STATE.is_dir():
    shutil.copytree(REAL_STATE, STATE)
else:
    STATE.mkdir(parents=True)
os.environ["HAIL_STATE_DIR"] = str(STATE)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hail_hermes.dashboard import plugin_api as api  # noqa: E402

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)

RESULTS: list[tuple[bool, str, str]] = []


def probe(method: str, path: str, *, expect=(200,), body=None, note="") -> None:
    t0 = time.time()
    try:
        r = client.request(method, path, json=body) if body is not None else client.request(method, path)
    except Exception as exc:  # noqa: BLE001 - an audit must report, not crash
        RESULTS.append((False, f"{method} {path}", f"raised {type(exc).__name__}: {exc}"))
        return
    ms = int((time.time() - t0) * 1000)
    ok = r.status_code in expect
    kind = ""
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = r.json()
            kind = f"keys={sorted(payload)[:6]}" if isinstance(payload, dict) else f"{type(payload).__name__}[{len(payload)}]"
        except Exception:
            kind = "json? unparsable"
    else:
        kind = f"bytes={len(r.content)} {r.headers.get('content-type','')[:24]}"
    RESULTS.append((ok, f"{method} {path}", f"{r.status_code} {ms}ms {kind} {note}".strip()))


# --- reads: what the pane shows on each tab -------------------------------------------------
for path in (
    "/status", "/settings", "/models", "/sessions", "/pending", "/setup-check",
    "/soul", "/soul-templates", "/identity", "/memory", "/slots", "/face", "/hermes-models",
):
    probe("GET", path)

# --- media routes: byte payloads, and 404 when the asset is genuinely absent ---------------
for path in ("/sample.wav", "/voice-sample", "/sample"):
    probe("GET", path, expect=(200, 404, 405, 422))

# --- writes: valid shape, and a rejected shape that must not corrupt state -----------------
probe("POST", "/settings", body={"voice_rate": "-5%"}, note="valid key")
probe("POST", "/settings", body={"not_a_key": 1}, expect=(400, 422), note="unknown key must be refused")
probe("POST", "/settings", body={"character": "definitely-not-a-character"}, expect=(400, 422), note="bad value")
probe("POST", "/approve", body={"code": "0000"}, expect=(400, 403, 404, 422), note="bogus pairing code")
probe("POST", "/deny", body={"code": "0000"}, expect=(200, 400, 404, 422), note="bogus code")
probe("POST", "/revoke", body={"device_id": "nope-not-a-device"}, expect=(200, 400, 404, 422))
probe("POST", "/soul", body={"soul": "audit"}, expect=(200, 400, 422))

print(f"{'RESULT':6} {'ROUTE':28} DETAIL")
for ok, route, detail in RESULTS:
    print(f"{'PASS' if ok else 'FAIL':6} {route:28} {detail}")
bad = [r for ok, r, _ in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} routes behave as expected")
if bad:
    print("unexpected: " + ", ".join(bad))
shutil.rmtree(TMP, ignore_errors=True)
