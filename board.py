"""The Playground board: Hermes composes it, the app renders it.

``app_board_publish`` validates a board against the catalogue in ``docs/contract/board.md``, stamps it
and replaces ``board.json`` in the data dir. ``GET /app/v1/board`` serves it.
"""
import json
import os
import re
from datetime import datetime, timezone

from . import store

TOOL = "app_board_publish"
MAX_SECTIONS, MAX_BLOCKS, MAX_STR, MAX_NOTE = 8, 40, 400, 1200
TONES = ("live", "info", "idle", "error")
TABS = ("sessions", "jobs", "host")
# type -> (required string fields, optional string fields)
BLOCKS = {
    "session": (("session_id", "title", "reason"), ()),
    "prompt": (("title", "prompt"), ("detail",)),
    "stat": (("label", "value"), ()),
    "note": (("text",), ()),
    "link": (("title", "url"), ("detail",)),
    "item": (("title",), ("detail", "time")),
}
_CRON_SESSION = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")

SCHEMA = {
    "name": TOOL,
    "description": (
        "Publish the Playground board shown in the Hermes app. Replaces the whole board. "
        "Load the skill hermes_app:board first for the block catalogue."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "board": {
                "type": "object",
                "description": "{title?, summary?, sections: [{title, blocks: [{type, ...}]}]}. "
                               "Block types: session, prompt, stat, note, link, item.",
            }
        },
        "required": ["board"],
    },
}


class BoardError(ValueError):
    pass


def _text(value, path: str, limit: int = MAX_STR) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoardError(f"{path} must be a non-empty string")
    return value.strip()[:limit]


def _opt(obj: dict, key: str, path: str, limit: int = MAX_STR):
    return _text(obj[key], f"{path}.{key}", limit) if obj.get(key) is not None else None


def _action(raw, path: str) -> dict:
    if not isinstance(raw, dict):
        raise BoardError(f"{path} must be an object")
    kind = raw.get("open")
    if kind == "session":
        return {"open": kind, "session_id": _text(raw.get("session_id"), f"{path}.session_id")}
    if kind == "prompt":
        return {"open": kind, "prompt": _text(raw.get("prompt"), f"{path}.prompt")}
    if kind == "url":
        return {"open": kind, "url": _text(raw.get("url"), f"{path}.url")}
    if kind == "tab":
        if raw.get("tab") not in TABS:
            raise BoardError(f"{path}.tab must be one of {', '.join(TABS)}")
        return {"open": kind, "tab": raw["tab"]}
    raise BoardError(f"{path}.open must be one of session, prompt, url, tab")


def _block(raw, path: str) -> dict:
    if not isinstance(raw, dict):
        raise BoardError(f"{path} must be an object")
    kind = raw.get("type")
    if kind not in BLOCKS:
        raise BoardError(f"{path}.type must be one of {', '.join(BLOCKS)}")
    required, optional = BLOCKS[kind]
    out = {"type": kind}
    for key in required:
        out[key] = _text(raw.get(key), f"{path}.{key}", MAX_NOTE if kind == "note" else MAX_STR)
    for key in optional:
        value = _opt(raw, key, path)
        if value is not None:
            out[key] = value
    if "time" in out:
        try:
            datetime.fromisoformat(out["time"].replace("Z", "+00:00"))
        except ValueError:
            raise BoardError(f"{path}.time must be ISO 8601") from None
    if raw.get("tone") is not None:
        if raw["tone"] not in TONES:
            raise BoardError(f"{path}.tone must be one of {', '.join(TONES)}")
        out["tone"] = raw["tone"]
    if raw.get("action") is not None:
        out["action"] = _action(raw["action"], f"{path}.action")
    return out


def validate(raw) -> dict:
    """The board without the stamped fields, or BoardError naming the first invalid field."""
    if not isinstance(raw, dict):
        raise BoardError("board must be an object")
    sections = raw.get("sections")
    if not isinstance(sections, list) or not sections:
        raise BoardError("board.sections must be a non-empty list")
    if len(sections) > MAX_SECTIONS:
        raise BoardError(f"board.sections has more than {MAX_SECTIONS} sections")
    out = {}
    for key in ("title", "summary"):
        value = _opt(raw, key, "board")
        if value is not None:
            out[key] = value
    total, out["sections"] = 0, []
    for i, sec in enumerate(sections):
        path = f"board.sections[{i}]"
        if not isinstance(sec, dict):
            raise BoardError(f"{path} must be an object")
        blocks = sec.get("blocks")
        if not isinstance(blocks, list):
            raise BoardError(f"{path}.blocks must be a list")
        total += len(blocks)
        if total > MAX_BLOCKS:
            raise BoardError(f"board has more than {MAX_BLOCKS} blocks")
        out["sections"].append({
            "title": _text(sec.get("title"), f"{path}.title"),
            "blocks": [_block(b, f"{path}.blocks[{j}]") for j, b in enumerate(blocks)],
        })
    return out


def job_id_of(session_id) -> str | None:
    match = _CRON_SESSION.match(session_id) if isinstance(session_id, str) else None
    return match.group(1) if match else None


def _path():
    return store._data_dir() / "board.json"


def publish(raw, session_id=None) -> int:
    """Validate, stamp and write the board. Returns the block count."""
    board = validate(raw)
    board["generated_at"] = datetime.now(timezone.utc).isoformat()
    board["job_id"] = job_id_of(session_id)
    board["session_id"] = session_id or None
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return sum(len(s["blocks"]) for s in board["sections"])


def read() -> dict | None:
    try:
        board = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return board if isinstance(board, dict) else None


def handle_publish(args: dict, session_id: str = "", **_) -> str:
    """Tool handler: ``args`` is ``{"board": {...}}``; Hermes passes the calling ``session_id``."""
    try:
        blocks = publish((args or {}).get("board"), session_id)
    except BoardError as e:
        return json.dumps({"ok": False, "error": str(e)})
    return json.dumps({"ok": True, "blocks": blocks})
