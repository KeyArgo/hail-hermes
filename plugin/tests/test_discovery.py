import asyncio
import hashlib
import json

import pytest

from hail_hermes.hub import PocketHub, _lan_ipv4_from_getaddrinfo
from hail_hermes.settings import read_settings, update_settings, validate
from hail_hermes.store import Store


def test_lan_addresses_from_getaddrinfo(monkeypatch):
    infos = [
        (2, 1, 6, '', ('192.168.1.10', 0)),
        (2, 1, 6, 'localhost.localdomain', ('127.0.0.1', 0)),
        (2, 1, 6, '', ('192.168.1.10', 0)),
    ]

    def fake_getaddrinfo(host, port, family=0):
        assert family == 2
        return infos

    monkeypatch.setattr('hail_hermes.hub.socket.getaddrinfo', fake_getaddrinfo)
    assert _lan_ipv4_from_getaddrinfo() == {'192.168.1.10'}


def test_lan_addresses_survives_getaddrinfo_error(monkeypatch):
    def boom(host, port, family=0):
        raise OSError('no hostname')

    monkeypatch.setattr('hail_hermes.hub.socket.getaddrinfo', boom)
    assert _lan_ipv4_from_getaddrinfo() == set()


@pytest.mark.asyncio
async def test_beacon_payload_and_interval(tmp_path):
    hub = PocketHub(store=Store(tmp_path))
    hub.port = 8767
    sent, delays = [], []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await hub._beacon_loop(sender=sent.append, sleep=sleep)
    assert delays == [2, 2]
    payload = json.loads(sent[0])
    assert payload == {"svc": "hail-hermes", "v": 1, "name": __import__('socket').gethostname(),
                       "port": 8767, "fp": hashlib.sha256(hub.store.hub_id().encode()).hexdigest()[:12]}
    assert sent[0] == sent[1]
    assert hub._control_token not in sent[0].decode()


@pytest.mark.asyncio
async def test_lan_toggle_adds_and_closes_listeners(tmp_path, monkeypatch):
    hub = PocketHub(store=Store(tmp_path))
    hub._server = object()
    hub.port = 8767
    servers = []

    class Server:
        closed = False
        def close(self):
            self.closed = True
        async def wait_closed(self):
            pass

    async def serve(*args, **kwargs):
        server = Server()
        servers.append(server)
        return server

    async def beacon():
        await asyncio.Future()

    monkeypatch.setattr('hail_hermes.hub._lan_ipv4_addresses', lambda: {'192.168.1.2'})
    monkeypatch.setattr('hail_hermes.hub.serve', serve)
    monkeypatch.setattr(hub, '_beacon_loop', beacon)
    assert read_settings(tmp_path)['lan_enabled'] is False
    await hub._sync_lan()
    assert not servers and hub._beacon_task is None
    update_settings(tmp_path, {'lan_enabled': True})
    await hub._reload_settings()
    task = hub._beacon_task
    assert len(servers) == 1 and task is not None
    await hub._sync_lan()
    assert len(servers) == 1
    update_settings(tmp_path, {'lan_enabled': False})
    await hub._reload_settings()
    assert servers[0].closed and task.cancelled() and hub._beacon_task is None
    with pytest.raises(ValueError):
        validate({'lan_enabled': 'false'})
