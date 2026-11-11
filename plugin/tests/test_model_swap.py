"""Changing the model on the watch must take effect on the next answer, not after a restart."""

from hail_hermes.agent import DeviceInfo, HermesCliAgent

DEVICE = DeviceInfo(device_id="d1", name="watch", kind="wear")


def _live(value):
    return lambda: value


def test_argv_uses_the_current_setting_not_the_one_from_startup():
    agent = HermesCliAgent(model="old-model", provider="old-provider",
                           model_lookup=_live(("GLM-5.3-Flash-EXL3", "sparks")))
    argv = agent.argv(DEVICE)
    assert "GLM-5.3-Flash-EXL3" in argv and "sparks" in argv
    assert "old-model" not in argv and "old-provider" not in argv


def test_a_resumed_session_still_keeps_its_own_model():
    # Resuming carries the session's model; passing -m would override what the session already is.
    agent = HermesCliAgent(model="m", provider="p", model_lookup=_live(("m2", "p2")))
    argv = agent.argv(DEVICE, session_id="sess-1")
    assert "--resume" in argv and "sess-1" in argv
    assert "-m" not in argv


def test_lookup_failure_falls_back_to_startup_values():
    def boom():
        raise RuntimeError("settings unreadable")

    agent = HermesCliAgent(model="fallback", provider="p", model_lookup=boom)
    argv = agent.argv(DEVICE)
    assert "fallback" in argv
