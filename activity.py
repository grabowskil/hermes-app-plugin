"""Live agent activity per session.

Agent hooks (``pre_llm_call``, ``pre_api_request``, ``post_api_request``, ``post_tool_call``,
``post_llm_call``, ``on_session_end``) feed an in-memory record per session, served by
``GET /app/v1/sessions/{id}/activity``. Turns without a ``/v1/runs`` run (the background review
fork shares the parent's session id) are covered too. State is lost on restart.

``active`` is true while a turn is open and a hook arrived in the last ``IDLE_SECONDS``, or while
the API server still holds a live run for the session. ``kind`` is ``run`` when such a run exists,
else ``background``. The run id comes from the API server adapter's ``_run_statuses`` (bound by
``routes.wire``).
"""
import logging
import threading
import time

from . import budget

log = logging.getLogger(__name__)

CAP = 50  # sessions kept; the oldest insertion is dropped first
IDLE_SECONDS = 90
LIVE_STATUSES = frozenset({"queued", "running", "waiting_for_approval", "stopping"})

_lock = threading.Lock()
_state: dict[str, dict] = {}
_adapter = None  # the API server adapter, set by bind()


def bind(adapter) -> None:
    """Remember the API server adapter so live run ids can be looked up."""
    global _adapter
    _adapter = adapter


def _new(turn_id: str, now: float) -> dict:
    return {"turn_id": turn_id, "ended": False, "api_calls": 0, "tool_calls": 0,
            "tokens_in": 0, "tokens_out": 0, "started_at": now, "updated_at": now}


def _touch(kwargs, end: bool = False, **add) -> None:
    """Record one event. An event on a missing or ended record opens a new turn."""
    session_id = kwargs.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    turn_id = kwargs.get("turn_id")
    turn_id = turn_id if isinstance(turn_id, str) else ""
    now = time.time()
    with _lock:
        rec = _state.get(session_id)
        if end:
            # A stale end (the parent's, after a fork opened a new turn) must not close the new one.
            if rec is not None and not rec["ended"] and (not turn_id or not rec["turn_id"] or turn_id == rec["turn_id"]):
                rec["ended"] = True
                rec["updated_at"] = now
            return
        if rec is None or rec["ended"]:
            if rec is None:
                while len(_state) >= CAP:
                    del _state[next(iter(_state))]
            rec = _state[session_id] = _new(turn_id, now)
        if kwargs.get("_new_turn"):
            rec.update(_new(turn_id, now))
        rec["updated_at"] = now
        for key, value in add.items():
            rec[key] += value


def _guard(fn):
    def hook(**kwargs):
        try:
            fn(kwargs)
        except Exception:
            log.exception("hermes_app: activity hook failed")

    hook.__name__ = fn.__name__
    return hook


@_guard
def on_pre_llm_call(kwargs) -> None:
    """Turn start: counters reset."""
    _touch({**kwargs, "_new_turn": True})


@_guard
def on_pre_api_request(kwargs) -> None:
    _touch(kwargs)


@_guard
def on_post_api_request(kwargs) -> None:
    usage = kwargs.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens_in = usage.get("prompt_tokens")
    tokens_out = usage.get("output_tokens")
    _touch(kwargs, api_calls=1,
           tokens_in=tokens_in if isinstance(tokens_in, int) else 0,
           tokens_out=tokens_out if isinstance(tokens_out, int) else 0)


@_guard
def on_post_tool_call(kwargs) -> None:
    _touch(kwargs, tool_calls=1)


@_guard
def on_turn_end(kwargs) -> None:
    _touch(kwargs, end=True)


def _live_run_id(session_id: str) -> str | None:
    """The newest live ``/v1/runs`` run of the session, or None."""
    statuses = getattr(_adapter, "_run_statuses", None)
    if not isinstance(statuses, dict):
        return None
    best, best_at = None, -1.0
    for run_id, status in list(statuses.items()):
        if (isinstance(status, dict) and status.get("session_id") == session_id
                and status.get("status") in LIVE_STATUSES):
            created = status.get("created_at")
            created = created if isinstance(created, (int, float)) else 0.0
            if created > best_at:
                best, best_at = str(run_id), created
    return best


def snapshot(session_id: str) -> dict:
    """The activity of a session. Never raises."""
    try:
        run_id = _live_run_id(session_id)
    except Exception:
        log.exception("hermes_app: could not look up the live run")
        run_id = None
    try:
        with _lock:
            rec = dict(_state.get(session_id) or {})
        fresh = bool(rec) and not rec["ended"] and time.time() - rec["updated_at"] < IDLE_SECONDS
        active = fresh or run_id is not None
        return {
            "active": active,
            "kind": ("run" if run_id else "background") if active else None,
            "run_id": run_id,
            "api_calls": rec.get("api_calls", 0),
            "tool_calls": rec.get("tool_calls", 0),
            "tokens_in": rec.get("tokens_in", 0),
            "tokens_out": rec.get("tokens_out", 0),
            "started_at": rec.get("started_at"),
            "updated_at": rec.get("updated_at"),
            "mode": budget.get_mode(session_id),
        }
    except Exception:
        log.exception("hermes_app: could not read activity")
        return {"active": False, "kind": None, "run_id": None, "api_calls": 0, "tool_calls": 0,
                "tokens_in": 0, "tokens_out": 0, "started_at": None, "updated_at": None, "mode": budget.DEFAULT_MODE}
