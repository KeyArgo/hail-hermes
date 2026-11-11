import json
from types import SimpleNamespace

from websockets.datastructures import Headers
from websockets.http11 import Request
from websockets.protocol import State

from hail_hermes.hub import PocketHub
from hail_hermes.store import Store


class Connection:
    def __init__(self, address):
        self.remote_address = (address, 1234)

    def respond(self, status, body):
        return SimpleNamespace(status_code=status, body=body, headers={})


async def test_status_is_loopback_only_and_redacted(state_dir, monkeypatch):
    store = Store(state_dir)
    token = store.issue_token('secret-id', 'Watch', 'watch')
    hub = PocketHub(store=store)
    calls = []

    async def offload(fn):
        calls.append(fn)
        return fn()

    monkeypatch.setattr('hail_hermes.hub.asyncio.to_thread', offload)
    request = Request('/status', Headers())
    forbidden = await hub._process_request(Connection('192.0.2.1'), request)
    assert forbidden.status_code == 403 and not calls
    response = await hub._process_request(Connection('127.0.0.1'), request)
    assert calls == [store.paired]
    data = json.loads(response.body)
    assert response.status_code == 200
    assert response.headers['Access-Control-Allow-Origin'] == '*'
    assert set(data) == {'running', 'version', 'mode', 'character', 'devices', 'pending'}
    assert data['devices'] == [{'name': 'Watch', 'kind': 'watch', 'connected': False, 'last_seen': None}]
    assert token not in response.body and 'secret-id' not in response.body and 'token' not in response.body


async def test_status_reports_only_public_pending_fields(state_dir, monkeypatch):
    import time

    hub = PocketHub(store=Store(state_dir))
    async def offload(fn):
        return fn()
    monkeypatch.setattr('hail_hermes.hub.asyncio.to_thread', offload)
    hub.pending['private-request'] = SimpleNamespace(
        device=SimpleNamespace(name='Watch'), match_code='4821',
        deadline=time.monotonic() + 30, ws=SimpleNamespace(state=State.OPEN))
    response = await hub._process_request(Connection('::1'), Request('/status', Headers()))
    pending = json.loads(response.body)['pending']
    assert len(pending) == 1
    assert set(pending[0]) == {'name', 'match_code', 'expires_in'}
    assert pending[0]['name'] == 'Watch' and pending[0]['match_code'] == '4821'
    assert 0 < pending[0]['expires_in'] <= 30
    assert 'private-request' not in response.body
