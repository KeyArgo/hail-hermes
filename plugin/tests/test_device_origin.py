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


# --- Quality review: the accepted set is explicit, and the desktop path is proven -----------------

import pytest
from websockets.exceptions import InvalidStatus

from conftest import Client, is_type, running_hub


def test_any_scheme_is_no_longer_an_origin():
    """The old fallback accepted every scheme it did not try to parse."""
    for origin in ("file://", "data:text/html,<h1>x</h1>", "blob:http://127.0.0.1:1/abc",
                   "chrome-extension://abcdefghijklmnop", "hermes://pane",
                   "http://127.0.0.1:8765.evil.example", "ftp://127.0.0.1:21"):
        assert origin == "file://" or not _allowed_device_origin(origin), origin


def test_a_remote_page_is_still_refused():
    for origin in ("https://example.com", "http://192.168.1.10:8765", "http://10.0.0.5:3000"):
        assert not _allowed_device_origin(origin), origin


def test_loopback_pages_and_the_desktop_are_still_welcome():
    for origin in ("http://127.0.0.1:8765", "http://localhost:3000", "https://127.0.0.1:4443",
                   "file://", "file:///Applications/Hermes.app/index.html"):
        assert _allowed_device_origin(origin), origin


async def test_the_desktop_origin_can_pair_and_talk(state_dir):
    """The packaged app's renderer connects from `file://`; that path has to keep working."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub, origin="file://") as c:
            await c.pair()
            await c.send("text", text="hello from the desk")
            await c.until(is_type("state", screen="idle"))


async def test_a_data_url_origin_is_refused_at_the_handshake(state_dir):
    async with running_hub(state_dir) as hub:
        with pytest.raises(InvalidStatus) as caught:
            async with Client.open(hub, origin="data:text/html,nope"):
                pass
    assert caught.value.response.status_code == 403


async def test_an_unknown_scheme_is_refused_at_the_handshake(state_dir):
    async with running_hub(state_dir) as hub:
        with pytest.raises(InvalidStatus) as caught:
            async with Client.open(hub, origin="hermes://elsewhere"):
                pass
    assert caught.value.response.status_code == 403


async def test_no_origin_header_is_still_a_device(state_dir):
    """The watch and adb send no Origin at all; they must not be collateral damage."""
    async with running_hub(state_dir) as hub:
        async with Client.open(hub) as c:
            await c.pair()
