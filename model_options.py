"""Model picker payload for ``GET /app/v1/model/options``.

The gateway's model options list marks every provider that lists models as authenticated, even when
its login is stale. Each such provider is checked with the resolver a run uses; a failure turns
``authenticated`` off and sets ``auth_error``. With a ``session_id``, the payload also names the
session's locked model and provider (read-only from ``state.db``).
"""
import json
import logging
import sqlite3
from typing import Optional

from . import files

logger = logging.getLogger(__name__)

SQL = "SELECT model_config FROM sessions WHERE id = ?"


def _check_providers(payload: dict) -> None:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    for row in payload.get("providers", []):
        if not (row.get("authenticated") and row.get("models")):
            continue
        try:
            resolve_runtime_provider(requested=row["slug"])
        except Exception as exc:
            row["authenticated"] = False
            row["auth_error"] = str(exc)[:200]


def _session_lock(session_id: str) -> Optional[dict]:
    """The session's ``browser_model_lock`` (``model``, ``provider``), or None. Never raises."""
    con = None
    try:
        con = sqlite3.connect(f"file:{files._hermes_home() / 'state.db'}?mode=ro", uri=True)
        row = con.execute(SQL, (session_id,)).fetchone()
        config = json.loads(row[0]) if row and row[0] else {}
        lock = config.get("browser_model_lock") if isinstance(config, dict) else None
    except (sqlite3.Error, ValueError):
        logger.warning("hermes_app: could not read the model lock of %r", session_id)
        return None
    finally:
        if con is not None:
            con.close()
    return lock if isinstance(lock, dict) else None


def build(session_id: Optional[str]) -> dict:
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    payload = build_model_options_payload(load_picker_context(), include_unconfigured=False, refresh=False)
    _check_providers(payload)
    lock = _session_lock(session_id) if session_id else None
    if lock:
        if lock.get("model"):
            payload["session_model"] = lock["model"]
        if lock.get("provider"):
            payload["session_provider"] = lock["provider"]
    return payload
