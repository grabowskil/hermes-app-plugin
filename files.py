"""Artefact files for ``GET /app/v1/files``.

An agent hands over a file by writing ``MEDIA:<absolute path>`` in a reply (the Hermes convention;
the API server stores the text unchanged). A path is served only when that exact tag occurs in an
assistant message of the requested session.
"""
import logging
import os
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SQL = "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' AND active = 1 AND content LIKE '%MEDIA:%'"
# The path ends at whitespace, closing punctuation, or a full stop that ends a sentence.
_END = r"""(?=[\s`'")\]>,;:!?]|\.?(?:\s|$)|$)"""


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def is_named(session_id: str, path: str) -> bool:
    """True when ``MEDIA:<path>`` is in an assistant message of the session. Never raises."""
    pattern = re.compile(r"MEDIA:[ \t]*" + re.escape(path) + _END)
    con = None
    try:
        con = sqlite3.connect(f"file:{_hermes_home() / 'state.db'}?mode=ro", uri=True)
        return any(isinstance(text, str) and pattern.search(text)
                   for (text,) in con.execute(SQL, (session_id,)))
    except sqlite3.Error:
        logger.exception("hermes_app: could not check files of %r", session_id)
        return False
    finally:
        if con is not None:
            con.close()
