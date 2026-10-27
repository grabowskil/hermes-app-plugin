"""Tool-call budget per turn for app (api_server) sessions.

``on_pre_tool_call`` counts tool calls per (session, turn) and blocks at the hard limit, and web or
browser tools at their own limit. ``on_transform_tool_result`` appends a note to each result from the
soft limit on. The mode (quick or deep) is set per session by ``PUT /app/v1/sessions/{id}/mode``; the
default is quick. Other platforms are untouched. Limits can be overridden in
``<HERMES_HOME>/hermes_app/budget.json`` as ``{"quick": {"soft", "hard", "web"}, "deep": {...}}``.
Counters live in memory only. Every hook fails open.
"""
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

MODES = ("quick", "deep")
DEFAULT_MODE = "quick"
LIMITS = {
    "quick": {"soft": 12, "hard": 25, "web": 4},
    "deep": {"soft": 30, "hard": 60, "web": 12},
}
NOTE = "[Budget reached: answer now with what you have, or ask the user one question.]"
CAP = 200  # counters and modes kept; the oldest insertion is dropped first
WEB_TOOLS = frozenset({"web_search", "web_extract"})

_lock = threading.Lock()
_modes: dict[str, str] = {}
_counts: dict[tuple[str, str], list[int]] = {}  # (session, turn) -> [calls, web calls]
_limits: dict[str, dict[str, int]] | None = None


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def _load_limits() -> dict[str, dict[str, int]]:
    limits = {mode: dict(values) for mode, values in LIMITS.items()}
    try:
        data = json.loads((_hermes_home() / "hermes_app" / "budget.json").read_text())
    except FileNotFoundError:
        return limits
    except (OSError, ValueError):
        log.exception("hermes_app: could not read budget.json")
        return limits
    if isinstance(data, dict):
        for mode in MODES:
            over = data.get(mode)
            if not isinstance(over, dict):
                continue
            for key in ("soft", "hard", "web"):
                value = over.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    limits[mode][key] = value
    return limits


def _limits_for(mode: str) -> dict[str, int]:
    global _limits
    with _lock:
        if _limits is None:
            _limits = _load_limits()
        return _limits[mode]


def set_mode(session_id: str, mode: str) -> str:
    """Set the budget mode of a session. Raises ValueError for an unknown mode."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    with _lock:
        if session_id not in _modes:
            while len(_modes) >= CAP:
                del _modes[next(iter(_modes))]
        _modes[session_id] = mode
    return mode


def get_mode(session_id: str) -> str:
    """The budget mode of a session; quick unless set."""
    with _lock:
        return _modes.get(session_id, DEFAULT_MODE)


def _is_web(tool_name: str) -> bool:
    return tool_name in WEB_TOOLS or tool_name.startswith("browser_")


def _key(kwargs) -> tuple[str, str] | None:
    """(session, turn) for an app session, else None. api_server session ids start with ``api_``."""
    session_id = kwargs.get("session_id")
    if not isinstance(session_id, str) or not session_id.startswith("api_"):
        return None
    turn_id = kwargs.get("turn_id")
    return session_id, turn_id if isinstance(turn_id, str) else ""


def on_pre_tool_call(**kwargs):
    """Block a call beyond the hard limit, or a web call beyond the web limit. Never raises."""
    try:
        key = _key(kwargs)
        if key is None:
            return None
        tool_name = kwargs.get("tool_name")
        web = isinstance(tool_name, str) and _is_web(tool_name)
        limits = _limits_for(get_mode(key[0]))
        with _lock:
            counts = _counts.get(key)
            if counts is None:
                while len(_counts) >= CAP:
                    del _counts[next(iter(_counts))]
                counts = _counts[key] = [0, 0]
            if counts[0] >= limits["hard"] or (web and counts[1] >= limits["web"]):
                return {"action": "block", "message": NOTE}
            counts[0] += 1
            if web:
                counts[1] += 1
        return None
    except Exception:
        log.exception("hermes_app: budget check failed")
        return None


def on_transform_tool_result(**kwargs):
    """Append the budget note to a result from the soft limit on. Never raises."""
    try:
        key = _key(kwargs)
        result = kwargs.get("result")
        if key is None or not isinstance(result, str):
            return None
        soft = _limits_for(get_mode(key[0]))["soft"]
        with _lock:
            counts = _counts.get(key)
            calls = counts[0] if counts else 0
        if calls >= soft:
            return result + "\n\n" + NOTE
        return None
    except Exception:
        log.exception("hermes_app: budget note failed")
        return None
