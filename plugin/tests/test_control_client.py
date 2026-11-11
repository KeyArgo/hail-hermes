import socket
import threading

import pytest

from hail_hermes.control import ControlClient, HubUnavailable
from hail_hermes.store import Store


def test_hub_that_drops_the_handshake_is_reported_unavailable(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def drop_every_connection():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.recv(4096)
            conn.close()

    threading.Thread(target=drop_every_connection, daemon=True).start()
    store = Store(tmp_path)
    store.control_token()
    store.write_live({"control_url": f"ws://127.0.0.1:{srv.getsockname()[1]}/control", "devices": []})
    try:
        with pytest.raises(HubUnavailable):
            ControlClient(tmp_path).call("pair_list")
    finally:
        srv.close()
