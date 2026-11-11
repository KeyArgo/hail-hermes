"""Session rows must carry the project folder, so the watch can say what it is resuming."""

from hail_hermes.agent import parse_sessions_table

TABLE = """Title                        Workspace          Last Active   ID
────────────────────────────────────────────────────────────────────
DESIGN ONLY - no source ed   Argo               6m ago        local-abc
Code review task, read onl   hail2              1h ago        local-def
"""


def test_folder_is_captured_for_each_session():
    rows = parse_sessions_table(TABLE)
    assert len(rows) == 2
    assert rows[0]["folder"] == "Argo"
    assert rows[1]["folder"] == "hail2"
    assert rows[0]["age"] == "6m ago"
    assert rows[0]["id"] == "local-abc"


def test_a_row_without_a_folder_still_parses():
    table = """Title                        Workspace   Last Active   ID
──────────────────────────────────────────────────
Just a title                 Argo        2d ago        local-xyz
"""
    rows = parse_sessions_table(table)
    assert rows and rows[0]["id"] == "local-xyz" and rows[0]["age"] == "2d ago"
