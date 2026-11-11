"""What was I doing in that session? Answered from Hermes' own session store.

No model call: the database already knows the project folder, the title and what the last
activity was. That keeps the answer instant and cheap enough to speak on a watch.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .models import hermes_home


def _db_path(home: Path | None = None) -> Path:
    return (home or hermes_home()) / "state.db"


def read_session_recap(session_id: str, home: Path | None = None, *, timeout: float = 2.0) -> dict[str, str] | None:
    """Return ``{folder, title, last, line}`` for a session, or ``None`` if it is gone.

    ``line`` is the one sentence a watch should speak, capped so it stays short aloud.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    db = _db_path(home)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select title, cwd, last_activity_description, ended_at from sessions where id = ?",
            (session_id,),
        ).fetchone()
        fallback = "" if (row is None or row["last_activity_description"]) else _last_words(conn, session_id)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None

    folder = str(row["cwd"] or "").strip()
    folder_name = os.path.basename(folder.rstrip("/\\")) or folder
    title = str(row["title"] or "").strip()
    # Only 2 of 156 sessions on the owner's machine carry `last_activity_description`, so a recap
    # built on it alone is usually a title with nothing after it — no answer to "where was I?".
    last = str(row["last_activity_description"] or "").strip() or fallback

    clean_title = title.rstrip("…").strip() if title not in ("-", "—") else ""
    head = f"Resuming {folder_name}" if folder_name else "Resuming your session"
    if clean_title:
        head += f": {clean_title}"
    line = head + (f". Last time: {_shorten(last, 120)}." if last else ".")
    return {
        "folder": folder_name,
        "title": title,
        "last": last,
        "line": line,
    }


def _last_words(conn: sqlite3.Connection, session_id: str, *, limit: int = 6) -> str:
    """What the session was actually doing, from its own last messages.

    Prefers the last thing the assistant said (that is where the work stands); falls back to the
    owner's last request when the final assistant turn is empty or tool-only. Tool output is
    skipped: a JSON blob read aloud is worse than saying nothing.
    """
    try:
        rows = conn.execute(
            "select role, content from messages where session_id = ? "
            "and role in ('assistant', 'user') and trim(coalesce(content, '')) <> '' "
            "order by timestamp desc limit ?",
            (session_id, limit),
        ).fetchall()
    except sqlite3.Error:
        return ""
    assistant = ""
    user = ""
    for row in rows:
        text = " ".join(str(row["content"] or "").split())
        if not text:
            continue
        if row["role"] == "assistant" and not assistant and not text.startswith("{"):
            assistant = text
        elif row["role"] == "user" and not user:
            user = text
        if assistant and user:
            break
    return _shorten(_speakable(assistant or user), 120)


def _speakable(text: str) -> str:
    """Strip what looks fine on screen and sounds wrong aloud.

    The recap is spoken by the watch, so `**bold**`, backticks, headings and stray quotes have to go
    before it reaches text-to-speech.
    """
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    cleaned = cleaned.replace("#", "").replace(">", "").replace("|", " ")
    cleaned = cleaned.strip().strip('"').strip("'").strip()
    return " ".join(cleaned.split())


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def recap_line(payload: dict[str, Any] | None) -> str:
    return str((payload or {}).get("line") or "")
