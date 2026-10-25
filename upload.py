"""Attachment storage for ``POST /app/v1/upload``.

The app uploads each pick as the raw body. The file lands in
``<hermes_home>/uploads/<YYYY-MM-DD>/<8 hex>-<name>`` and the reply carries its
absolute path, which goes into the run input for ``vision_analyze`` or the file tools.
"""
import os
import re
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import unquote

MAX_BYTES = 10_000_000  # the API server rejects larger bodies itself
FALLBACK_NAME = "file"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def sanitize_name(raw: str) -> str:
    """Basename with only [A-Za-z0-9._-]; other runs become '-'. Never empty."""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE.sub("-", base).strip(".-")
    return name or FALLBACK_NAME


def save(body: bytes, header_name: str, mime: str) -> dict:
    """Write one upload and return the 201 body. ``header_name`` is the percent-encoded X-Filename."""
    name = sanitize_name(unquote(header_name or ""))
    folder = _hermes_home() / "uploads" / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"{secrets.token_hex(4)}-{name}"
    path = folder / stored
    path.write_bytes(body)
    return {"path": str(path.absolute()), "name": stored, "size": len(body), "mime": mime}
