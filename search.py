"""Message search for ``GET /app/v1/search``.

Read-only over ``state.db``. ``messages_fts`` is an FTS5 table whose rowid is
``messages.id``: the insert and update triggers write ``new.id``. It joins to
``messages`` on that. Only user and assistant rows are searched.
"""
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

MIN_CHARS = 2
DEFAULT_LIMIT = 30
MAX_LIMIT = 50

SQL = """
SELECT m.session_id, COALESCE(s.title, ''), m.id, m.role,
       snippet(messages_fts, 0, char(2), char(3), '…', 12), m.timestamp
FROM messages_fts
JOIN messages m ON m.id = messages_fts.rowid
JOIN sessions s ON s.id = m.session_id
WHERE messages_fts MATCH ?
  AND m.role IN ('user', 'assistant')
  AND m.active = 1
ORDER BY messages_fts.rank
LIMIT ?
"""


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def fts_query(q: str) -> str:
    """Each term quoted, so FTS5 syntax can't break the query; the last term is a prefix."""
    quoted = ['"' + term.replace('"', '""') + '"' for term in q.split()]
    if quoted:
        quoted[-1] += "*"
    return " ".join(quoted)


def query(q: str, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Search hits, best match first. Short queries and sqlite errors give []. Never raises."""
    q = (q or "").strip()
    if len(q) < MIN_CHARS:
        return []
    limit = min(max(1, limit), MAX_LIMIT)
    con = None
    try:
        con = sqlite3.connect(f"file:{_hermes_home() / 'state.db'}?mode=ro", uri=True)
        rows = con.execute(SQL, (fts_query(q), limit)).fetchall()
    except sqlite3.Error:
        logger.exception("hermes_app: search failed for %r", q)
        return []
    finally:
        if con is not None:
            con.close()
    return [
        {
            "session_id": session_id,
            "session_title": title,
            "message_id": message_id,
            "role": role,
            "snippet": snippet.replace("**", "").replace("\x02", "**").replace("\x03", "**"),
            "timestamp": timestamp,
        }
        for session_id, title, message_id, role, snippet, timestamp in rows
    ]
