"""The device endpoint must not accept an anonymous origin, and `/status` must not be world-readable.

Review item 4. `origin == "null"` was accepted by `_allowed_device_origin`, and `/status` carried
`Access-Control-Allow-Origin: *`. `null` is the origin a sandboxed iframe, a `file://` page and a
`data:` URL all send — it identifies nothing — and a wildcard on a loopback route let any web page in
the browser read the hub's device list.

These fail on the reviewed tree (the origin was allowed, the header was sent) and pass once both are
gone, with the pane reading the route through `ctx.rest('/status')` instead.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from websockets.datastructures import Headers
from websockets.http11 import Request

from hail_hermes.hub import PocketHub, _allowed_device_origin
from hail_hermes.store import Store


class Connection:
    def __init__(self, address):
        self.remote_address = (address, 1234)

    def respond(self, status, body):
        return SimpleNamespace(status_code=status, body=body, headers={})


async def test_the_null_origin_is_not_a_trusted_origin():
    assert not _allowed_device_origin("null"), (
        "'null' is what a sandboxed frame and a file:// page send; it identifies no one"
    )


async def test_a_loopback_page_is_still_welcome():
    """The pane legitimately arrives from a loopback page, so this must keep working."""
    assert _allowed_device_origin("http://127.0.0.1:8765")
    assert _allowed_device_origin("http://localhost:8765")


async def test_a_pocket_request_with_the_null_origin_is_refused(state_dir):
    store = Store(state_dir)
    hub = PocketHub(store=store)
    headers = Headers()
    headers["Origin"] = "null"
    response = await hub._process_request(Connection("127.0.0.1"), Request("/pocket", headers))
    assert response is not None and response.status_code == 403, (
        f"a null-origin client was admitted: {response!r}"
    )


async def test_status_sends_no_wildcard_cors_header(state_dir):
    hub = PocketHub(store=Store(state_dir))
    response = await hub._process_request(Connection("127.0.0.1"), Request("/status", Headers()))
    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers
    assert set(json.loads(response.body)) == {"running", "version", "mode", "character", "devices", "pending"}
