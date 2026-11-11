"""Cross-process device-state read-modify-write tests."""

from __future__ import annotations

import multiprocessing

import pytest

from hail_hermes.store import Store


def _paused_save(state_dir, operation, ready, release):
    store = Store(state_dir)
    original_save = store._save

    def paused_save():
        ready.set()
        if not release.wait(10):
            raise TimeoutError("save was not released")
        original_save()

    store._save = paused_save
    if operation == "session":
        store.set_agent_session("device", "new-session")
    else:
        store.issue_token("other-device", "Other", "sim")


def _revoke(state_dir, started, finished):
    store = Store(state_dir)
    started.set()
    store.revoke("device")
    finished.set()


@pytest.mark.parametrize("operation", ["session", "token"])
def test_revoke_cannot_be_overwritten_by_another_process(
    state_dir, operation
):
    store = Store(state_dir)
    token = store.issue_token("device", "Device", "sim")

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    revoke_started = ctx.Event()
    revoke_finished = ctx.Event()
    writer = ctx.Process(
        target=_paused_save,
        args=(state_dir, operation, ready, release),
    )
    revoker = ctx.Process(
        target=_revoke,
        args=(state_dir, revoke_started, revoke_finished),
    )

    writer.start()
    try:
        assert ready.wait(5), "writer did not reach its save"
        revoker.start()
        try:
            # In the locked implementation the revoker may block in Store's
            # constructor before it can signal that it has started.
            revoke_started.wait(1)
            revoke_finished.wait(1)
        finally:
            release.set()
            revoker.join(10)
            if revoker.is_alive():
                revoker.terminate()
                revoker.join()
        assert revoker.exitcode == 0
    finally:
        release.set()
        writer.join(10)
        if writer.is_alive():
            writer.terminate()
            writer.join()
    assert writer.exitcode == 0

    reloaded = Store(state_dir)
    assert not reloaded.check_token("device", token)
    assert "device" not in reloaded.paired()
    if operation == "token":
        assert "other-device" in reloaded.paired()


def test_default_state_dir_compatibility(monkeypatch, tmp_path):
    from hail_hermes.store import default_state_dir

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HAIL_HERMES_STATE", raising=False)
    monkeypatch.delenv("HERMES_POCKET_STATE", raising=False)
    assert default_state_dir() == tmp_path / "hail-hermes"
    (tmp_path / "pocket").mkdir()
    assert default_state_dir() == tmp_path / "pocket"
    monkeypatch.setenv("HERMES_POCKET_STATE", str(tmp_path / "old-override"))
    assert default_state_dir() == tmp_path / "old-override"
    monkeypatch.setenv("HAIL_HERMES_STATE", str(tmp_path / "new-override"))
    assert default_state_dir() == tmp_path / "new-override"





def test_corrupt_devices_file_is_set_aside_even_when_an_old_one_exists(tmp_path):
    from hail_hermes.store import Store

    Store(tmp_path)
    (tmp_path / "devices.json").write_text("{not json")
    (tmp_path / "devices.corrupt").write_text("older corrupt copy")
    Store(tmp_path)
    assert (tmp_path / "devices.corrupt").read_text() == "{not json"
