"""Host-defined share actions for ``GET /app/v1/share/actions``.

Read from ``<HERMES_HOME>/hermes_app/share_actions.yaml``; see the README for the format.
"""
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

MATCHES = ("url", "text", "image", "any")


def _path() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes") / "hermes_app" / "share_actions.yaml"


def _action(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    fields = {key: raw.get(key) for key in ("id", "label", "prompt")}
    if not all(isinstance(v, str) and v.strip() for v in fields.values()):
        return None
    match = raw.get("match", "any")
    if match not in MATCHES:
        return None
    return {**fields, "match": match}


def load() -> Dict[str, Any]:
    """``{"decide_prompt", "actions"}``. A missing or unreadable file gives the empty answer."""
    empty: Dict[str, Any] = {"decide_prompt": None, "actions": []}
    try:
        data = yaml.safe_load(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, yaml.YAMLError):
        logger.exception("hermes_app: could not read share_actions.yaml")
        return empty
    if not isinstance(data, dict):
        return empty
    decide = data.get("decide_prompt")
    raw_actions = data.get("actions")
    actions: List[Dict[str, str]] = [a for a in map(_action, raw_actions if isinstance(raw_actions, list) else []) if a]
    return {"decide_prompt": decide if isinstance(decide, str) and decide.strip() else None, "actions": actions}
