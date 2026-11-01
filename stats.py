"""Usage counters for ``GET /app/v1/stats``.

Read-only over ``state.db``. The last seven days are counted from the session start time and the
message timestamp (epoch seconds). Tool calls are the ``tool`` rows of the active transcript.
"""
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

WEEK = 7 * 24 * 3600
EMPTY = {"sessions_7d": 0, "messages_7d": 0, "tool_calls_7d": 0, "sessions_total": 0}


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def snapshot() -> Dict[str, int]:
    """The four counters. A missing database or a sqlite error gives zeros. Never raises."""
    since = time.time() - WEEK
    con = None
    try:
        con = sqlite3.connect(f"file:{_hermes_home() / 'state.db'}?mode=ro", uri=True)
        sessions_7d = con.execute("SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (since,)).fetchone()[0]
        sessions_total = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        messages_7d, tool_calls_7d = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(role = 'tool'), 0) FROM messages WHERE timestamp >= ? AND active = 1",
            (since,),
        ).fetchone()
    except sqlite3.Error:
        logger.exception("hermes_app: could not read stats")
        return dict(EMPTY)
    finally:
        if con is not None:
            con.close()
    return {"sessions_7d": sessions_7d, "messages_7d": messages_7d, "tool_calls_7d": tool_calls_7d,
            "sessions_total": sessions_total}
