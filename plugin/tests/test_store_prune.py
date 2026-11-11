"""Stale device pruning: only old offline sim/test probes go; a real watch stays."""

from __future__ import annotations

import json
import time

from hail_hermes.store import Store


def _age(state_dir, device_id: str, days: float, *, seen: bool = False) -> None:
    """Backdate one device entry, either its last_seen or its paired_at."""
    path = state_dir / "devices.json"
    data = json.loads(path.read_text())
    entry = data["devices"][device_id]
    old = time.time() - days * 24 * 60 * 60
    if seen:
        entry["last_seen"] = old
    else:
        entry.pop("last_seen", None)
        entry["paired_at"] = old
    path.write_text(json.dumps(data))


def _devices(state_dir) -> dict:
    return json.loads((state_dir / "devices.json").read_text())["devices"]


def test_30_day_old_sim_device_is_pruned(state_dir):
    Store(state_dir).issue_token("probe", "feature-check", "sim")
    _age(state_dir, "probe", 30)

    removed = Store(state_dir).prune()

    assert removed == ["probe"]
    assert "probe" not in _devices(state_dir)


def test_30_day_old_wear_device_is_kept(state_dir):
    Store(state_dir).issue_token("watch", "WearOS Watch", "wear")
    _age(state_dir, "watch", 30)

    removed = Store(state_dir).prune()

    assert removed == []
    assert "watch" in Store(state_dir).paired()


def test_connected_sim_device_is_never_pruned(state_dir):
    store = Store(state_dir)
    store.issue_token("probe", "feature-check", "sim")
    _age(state_dir, "probe", 30)
    store.write_live({"devices": [{"device_id": "probe"}]})

    assert Store(state_dir).prune() == []
    assert "probe" in Store(state_dir).paired()


def test_recent_sim_device_is_kept(state_dir):
    Store(state_dir).issue_token("probe", "feature-check", "sim")

    assert Store(state_dir).prune() == []


def test_check_token_records_last_seen(state_dir):
    store = Store(state_dir)
    token = store.issue_token("probe", "feature-check", "sim")
    _age(state_dir, "probe", 30, seen=True)

    assert Store(state_dir).check_token("probe", token)

    seen = _devices(state_dir)["probe"]["last_seen"]
    assert seen > time.time() - 60


def test_revoke_undoes_a_probe_pairing(state_dir):
    """What a fresh feature_check run does: pair a probe, then revoke it again."""
    Store(state_dir).issue_token("watch", "WearOS Watch", "wear")
    before = len(Store(state_dir).paired())

    Store(state_dir).issue_token("probe", "feature-check", "sim")
    assert len(Store(state_dir).paired()) == before + 1

    assert Store(state_dir).revoke("probe")
    assert len(Store(state_dir).paired()) == before
