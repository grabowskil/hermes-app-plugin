"""Incremental message reads for ``GET /app/v1/sessions/{id}/messages``.

Read-only over ``state.db``. Fields match the gateway's ``GET /api/sessions/{id}/messages`` rows
(``_message_response``), so the app can reuse its decoder. Tool output is cut so one page stays
small; the app reads the full text from the gateway when it needs it.
"""
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
TOOL_CHARS = 1500
JSON_PREFIX = "\x00json:"  # hermes_state: marks JSON-encoded structured content

SQL = """
SELECT id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
       token_count, finish_reason, reasoning, reasoning_content, display_kind
FROM messages
WHERE session_id = ? AND id > ? AND active = 1
ORDER BY id
LIMIT ?
"""


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def _json_or(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return default


def _message(row: tuple) -> Dict[str, Any]:
    (id_, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
     token_count, finish_reason, reasoning, reasoning_content, display_kind) = row
    if isinstance(content, str) and content.startswith(JSON_PREFIX):
        content = _json_or(content[len(JSON_PREFIX):], content)
    if display_kind == "hidden":
        content = ""
    message = {
        "id": id_, "session_id": session_id, "role": role, "content": content,
        "tool_call_id": tool_call_id,
        "tool_calls": _json_or(tool_calls, []) if tool_calls else None,
        "tool_name": tool_name, "timestamp": timestamp, "token_count": token_count,
        "finish_reason": finish_reason, "reasoning": reasoning,
        "reasoning_content": reasoning_content, "display_kind": display_kind,
    }
    if role == "tool":
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if text is not None and len(text) > TOOL_CHARS:
            message["content"] = text[:TOOL_CHARS]
            message["truncated"] = True
    return message


def messages_after(session_id: str, after_id: int = 0, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Up to ``limit`` active messages with id > ``after_id``, oldest first, plus ``last_id``
    (``after_id`` when nothing is new). sqlite errors give an empty page. Never raises."""
    limit = min(max(1, limit), MAX_LIMIT)
    after_id = max(0, after_id)
    con = None
    try:
        con = sqlite3.connect(f"file:{_hermes_home() / 'state.db'}?mode=ro", uri=True)
        rows = con.execute(SQL, (session_id, after_id, limit)).fetchall()
        data = [_message(row) for row in rows]
    except sqlite3.Error:
        logger.exception("hermes_app: could not read messages of %r", session_id)
        return {"data": [], "last_id": after_id}
    finally:
        if con is not None:
            con.close()
    return {"data": data, "last_id": data[-1]["id"] if data else after_id}
