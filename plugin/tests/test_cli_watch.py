import subprocess

from hail_hermes import cli


def test_watch_helper_gives_up_when_adb_hangs(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "adb")

    def hanging_adb(argv, **kwargs):
        # A wedged adb server never answers; without a timeout this call would block forever.
        if kwargs.get("timeout") is None:
            raise AssertionError("adb was called without a timeout")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(cli.subprocess, "run", hanging_adb)
    assert cli._watch(None) == 1
    assert "did not answer" in capsys.readouterr().err


def test_bad_config_file_gives_a_plain_message(tmp_path):
    import pytest

    missing = tmp_path / "nope.json"
    with pytest.raises(SystemExit, match="Could not read config file"):
        cli.load_config(str(missing))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(SystemExit, match="not valid JSON"):
        cli.load_config(str(broken))
