"""Notice classification and JSON storage for the hermes_app plugin."""
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

CRON_PREFIX = "Cronjob Response: "
SEPARATOR = "-------------"
TRAILER = "To stop or manage this job"
_JOB_ID = re.compile(r"\(job_id:\s*([^)\s]+)\s*\)")


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_APP_DATA_DIR", Path.home() / ".hermes" / "hermes_app"))


def __getattr__(name):
    # DATA_DIR is computed on access so tests can set HERMES_APP_DATA_DIR first.
    if name == "DATA_DIR":
        return _data_dir()
    raise AttributeError(name)


def classify(content: str) -> dict:
    notice = {
        "id": uuid.uuid4().hex,
        "kind": "gateway",
        "title": "Gateway",
        "body": content.strip(),
        "job_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if not content.startswith(CRON_PREFIX):
        return notice
    lines = content.split("\n")
    match = _JOB_ID.search(lines[1]) if len(lines) > 1 else None
    sep = next((i for i, line in enumerate(lines) if line.strip() == SEPARATOR), None)
    start = sep + 1 if sep is not None else 2
    end = next(
        (i for i in range(len(lines) - 1, start - 1, -1) if lines[i].lstrip().startswith(TRAILER)),
        len(lines),
    )
    notice.update(
        kind="cron",
        title=lines[0][len(CRON_PREFIX):].strip(),
        body="\n".join(lines[start:end]).strip(),
        job_id=match.group(1) if match else None,
    )
    return notice


def append(notice: dict) -> None:
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "notices.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(notice, ensure_ascii=False) + "\n")


def latest(limit: int = 100) -> list[dict]:
    path = _data_dir() / "notices.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    out: list[dict] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _devices_path(kind: str = "alert") -> Path:
    # Widget tokens live apart so alert pushes never reach them.
    return _data_dir() / ("widget_devices.json" if kind == "widget" else "devices.json")


def _write_devices(devices: list[dict], kind: str = "alert") -> None:
    path = _devices_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(devices, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_devices(kind: str = "alert") -> list[dict]:
    path = _devices_path(kind)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def upsert_device(token: str, environment: str, kind: str = "alert") -> None:
    devices = load_devices(kind)
    entry = {"token": token, "environment": environment}
    for i, d in enumerate(devices):
        if d.get("token") == token:
            devices[i] = entry
            break
    else:
        devices.append(entry)
    _write_devices(devices, kind)


def remove_devices(tokens: list[str], kind: str = "alert") -> None:
    drop = set(tokens)
    _write_devices([d for d in load_devices(kind) if d.get("token") not in drop], kind)
