"""Resuming an old session must say where it was and what it was about, without a model call."""

import sqlite3

from hail_hermes.recap import read_session_recap


def _make_db(tmp_path, rows):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table sessions (id text primary key, title text, cwd text, "
        "last_activity_description text, ended_at text)"
    )
    conn.executemany("insert into sessions values (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return tmp_path


def test_recap_names_the_folder_and_the_title(tmp_path):
    home = _make_db(tmp_path, [
        ("s1", "Fix the parser", "/work/hail2", "edited parser.py", None),
    ])
    info = read_session_recap("s1", home=home)
    assert info["folder"] == "hail2"
    assert info["line"].startswith("Resuming hail2: Fix the parser")
    assert "edited parser.py" in info["line"]


def test_recap_without_a_title_still_reads_sensibly(tmp_path):
    home = _make_db(tmp_path, [("s2", "—", "/work/app", "", None)])
    info = read_session_recap("s2", home=home)
    assert info["line"] == "Resuming app."


def test_missing_session_returns_nothing(tmp_path):
    home = _make_db(tmp_path, [("s3", "x", "/w", "", None)])
    assert read_session_recap("nope", home=home) is None
    assert read_session_recap("", home=home) is None


def test_a_long_activity_line_is_trimmed_for_speech(tmp_path):
    long = "worked on " + ("a very long explanation " * 20)
    home = _make_db(tmp_path, [("s4", "Big job", "/w/proj", long, None)])
    line = read_session_recap("s4", home=home)["line"]
    assert len(line) < 220 and line.endswith(".")
