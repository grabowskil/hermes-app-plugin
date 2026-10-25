"""Latest agent plan per session.

``on_post_tool_call`` fires after every tool call. For the todo tool (``todo_list``, older
``todo``) it keeps the latest full list in memory, served by ``GET /app/v1/todos``. The list is lost
on restart; the app then falls back to the transcript.
"""
import json
import logging
import threading

log = logging.getLogger(__name__)

TOOL_NAMES = frozenset({"todo_list", "todo"})
CAP = 50  # sessions kept; the oldest insertion is dropped first

_lock = threading.Lock()
_latest: dict[str, list] = {}


def on_post_tool_call(**kwargs) -> None:
    """Store the todo list from a todo tool result. Never raises."""
    try:
        if kwargs.get("tool_name") not in TOOL_NAMES:
            return
        session_id = kwargs.get("session_id")
        result = kwargs.get("result")
        if not isinstance(session_id, str) or not session_id or not isinstance(result, str):
            return
        try:
            obj = json.loads(result)
        except ValueError:
            return
        if not isinstance(obj, dict) or not isinstance(obj.get("todos"), list):
            return
        with _lock:
            if session_id not in _latest:
                while len(_latest) >= CAP:
                    del _latest[next(iter(_latest))]
            _latest[session_id] = obj["todos"]
    except Exception:
        log.exception("hermes_app: could not store todo list")


def latest(session_id: str) -> list | None:
    """The latest todo list for a session, or None. Never raises."""
    try:
        with _lock:
            todos = _latest.get(session_id)
            return list(todos) if todos is not None else None
    except Exception:
        log.exception("hermes_app: could not read todo list")
        return None
