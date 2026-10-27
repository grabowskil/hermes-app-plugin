"""Generic response-profile capability for the Hermes App plugin.

The plugin does NOT name any specific skill. Capability is discovered by scanning every
installed skill directory for a small manifest file (``hermes_app_profiles.json``).
The manifest carries the user-facing label, sub-label and the selectable profiles, plus
a payload source for each profile that points into the same skill directory. When a
manifest is found the catalog is exposed; when no skill ships one the toggle is hidden.

The actual prompt payloads live in the skill directory on the host, never in this repo.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "hermes_app_profiles.json"
MAX_PROFILES = 32
MAX_LABEL_CHARS = 80
MAX_ID_CHARS = 64
MAX_DESCRIPTION_CHARS = 400
MAX_PAYLOAD_CHARS = 32_000

DEFAULT_SKILL_ROOTS: tuple[str, ...] = ("~/.hermes/skills",)


@dataclass(frozen=True)
class ProfileDef:
    id: str
    title: str
    subtitle: str | None
    description: str | None
    default_on: bool
    source: str


@dataclass(frozen=True)
class ProfileCatalog:
    available: bool
    label: str | None
    label_sub: str | None
    skill_dir: Path | None
    profiles: tuple[ProfileDef, ...]


def _expand_home(p: str) -> Path:
    return Path(os.path.expanduser(p))


def _candidate_skill_roots() -> list[Path]:
    env = os.environ.get("HERMES_HOME")
    roots: list[Path] = []
    if env:
        roots.append(_expand_home(env) / "skills")
    for default in DEFAULT_SKILL_ROOTS:
        path = _expand_home(default)
        if path not in roots:
            roots.append(path)
    return [r for r in roots if r.is_dir()]


def _is_safe_id(value: str) -> bool:
    if not value or len(value) > MAX_ID_CHARS:
        return False
    if ".." in value or "/" in value or "\\" in value:
        return False
    return True


def _coerce_str(value: Any, cap: int) -> str | None:
    if isinstance(value, str):
        return value[:cap] if cap > 0 else value
    return None


def _validate_profile(raw: dict[str, Any]) -> ProfileDef | None:
    if not isinstance(raw, dict):
        return None
    pid = raw.get("id")
    title = raw.get("title")
    source = raw.get("source")
    if not isinstance(pid, str) or not isinstance(title, str) or not isinstance(source, str):
        return None
    pid = pid.strip()
    title = title.strip()
    source = source.strip()
    if not _is_safe_id(pid) or not title or not source:
        return None
    subtitle = _coerce_str(raw.get("subtitle"), MAX_LABEL_CHARS)
    description = _coerce_str(raw.get("description"), MAX_DESCRIPTION_CHARS)
    default_on = bool(raw.get("default_on", False))
    return ProfileDef(
        id=pid,
        title=title[:MAX_LABEL_CHARS],
        subtitle=subtitle,
        description=description,
        default_on=default_on,
        source=source,
    )


def discover_catalog(roots: Iterable[Path] | None = None) -> ProfileCatalog:
    """Return the first catalog any installed skill provides, or an empty one."""
    scan_roots = list(roots) if roots is not None else _candidate_skill_roots()
    for root in scan_roots:
        if not root.is_dir():
            continue
        try:
            manifests = sorted(root.rglob(MANIFEST_FILENAME))
        except OSError as exc:
            logger.debug("hermes_app: cannot scan %s (%s)", root, exc)
            continue
        for manifest_path in manifests:
            skill_dir = manifest_path.parent
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("hermes_app: bad manifest %s (%s)", manifest_path, exc)
                continue
            if not isinstance(raw, dict):
                continue
            profiles_in = raw.get("profiles") or []
            profiles: list[ProfileDef] = []
            if isinstance(profiles_in, list):
                for entry in profiles_in[:MAX_PROFILES]:
                    prof = _validate_profile(entry)
                    if prof is not None:
                        profiles.append(prof)
            if not profiles:
                continue
            label = _coerce_str(raw.get("label"), MAX_LABEL_CHARS)
            label_sub = _coerce_str(raw.get("label_sub"), MAX_LABEL_CHARS)
            return ProfileCatalog(
                available=True,
                label=label,
                label_sub=label_sub,
                skill_dir=skill_dir,
                profiles=tuple(profiles),
            )
    return ProfileCatalog(available=False, label=None, label_sub=None, skill_dir=None, profiles=())


def catalog_to_dict(catalog: ProfileCatalog) -> dict[str, Any]:
    return {
        "available": catalog.available,
        "label": catalog.label,
        "label_sub": catalog.label_sub,
        "profiles": [
            {
                "id": p.id,
                "title": p.title,
                "subtitle": p.subtitle,
                "description": p.description,
                "default_on": p.default_on,
            }
            for p in catalog.profiles
        ],
    }


def _safe_resolve(skill_dir: Path, source: str) -> Path | None:
    try:
        target = (skill_dir / source).resolve(strict=False)
        target.relative_to(skill_dir.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return target


def assemble_payload(catalog: ProfileCatalog, profile_ids: list[str]) -> str:
    """Concatenate selected profile payloads into one block, capped."""
    if not catalog.available or catalog.skill_dir is None or not profile_ids:
        return ""
    chosen: list[ProfileDef] = []
    seen: set[str] = set()
    for pid in profile_ids:
        if pid in seen:
            continue
        seen.add(pid)
        match = next((p for p in catalog.profiles if p.id == pid), None)
        if match is not None:
            chosen.append(match)
    parts: list[str] = []
    total = 0
    for prof in chosen:
        path = _safe_resolve(catalog.skill_dir, prof.source)
        if path is None or not path.is_file():
            logger.warning("hermes_app: missing payload for profile %s", prof.id)
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        if total + len(text) > MAX_PAYLOAD_CHARS:
            remaining = MAX_PAYLOAD_CHARS - total
            if remaining <= 0:
                break
            text = text[:remaining]
        parts.append(f"# {prof.title}\n{text}")
        total += len(text)
        if total >= MAX_PAYLOAD_CHARS:
            break
    return "\n\n".join(parts)


def inject_into_request(request: dict[str, Any], payload: str) -> dict[str, Any]:
    """Append the payload to the system prompt for this turn.

    Handles two provider shapes:
      * Anthropic: ``request["system"]`` is a string or list of content blocks.
      * OpenAI wire: the system message is ``request["messages"][0]`` with role ``system``.
    Falls back to prepending a new system message when neither slot exists.
    """
    if not payload:
        return request
    out = dict(request)

    if "system" in out:
        existing = out["system"]
        if isinstance(existing, str):
            out["system"] = (existing + "\n\n" + payload) if existing else payload
        elif isinstance(existing, list):
            out["system"] = list(existing) + [{"type": "text", "text": payload}]
        else:
            out["system"] = payload
        return out

    messages = out.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict) and first.get("role") == "system":
            content = first.get("content")
            new_messages = list(messages)
            if isinstance(content, str):
                new_messages[0] = {
                    **first,
                    "content": (content + "\n\n" + payload) if content else payload,
                }
            else:
                new_messages[0] = {**first, "content": payload}
            out["messages"] = new_messages
            return out

    if isinstance(messages, list):
        out["messages"] = [{"role": "system", "content": payload}] + list(messages)
    else:
        out["messages"] = [{"role": "system", "content": payload}]
    return out


# ---------------------------------------------------------------------------
# Per-session state store (JSON file under the plugin's data dir).
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    enabled: bool = False
    profile_ids: list[str] | None = None


class SessionStore:
    """Tiny JSON store keyed by session id. Thread-safe via a lock."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            if self._path.is_file():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("hermes_app: bad mode store %s (%s)", self._path, exc)
                    self._data = {}
            self._loaded = True

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("hermes_app: cannot persist mode store %s (%s)", self._path, exc)

    def get(self, session_id: str, *, default_ids: list[str] | None = None) -> SessionState:
        self._ensure_loaded()
        entry = self._data.get(session_id)
        if not isinstance(entry, dict):
            return SessionState(enabled=False, profile_ids=list(default_ids) if default_ids else None)
        ids_raw = entry.get("profile_ids")
        ids = [str(x) for x in ids_raw] if isinstance(ids_raw, list) else None
        return SessionState(enabled=bool(entry.get("enabled", False)), profile_ids=ids)

    def set(self, session_id: str, state: SessionState) -> None:
        self._ensure_loaded()
        with self._lock:
            self._data[session_id] = {
                "enabled": bool(state.enabled),
                "profile_ids": list(state.profile_ids) if state.profile_ids is not None else [],
            }
            self._persist()


_store: SessionStore | None = None


def configure_store(data_dir: Path) -> SessionStore:
    """Wire the singleton store. Called once from register()."""
    global _store
    _store = SessionStore(data_dir / "session_modes.json")
    return _store


def get_store() -> SessionStore:
    if _store is None:
        raise RuntimeError("hermes_app: profile store not configured; call configure_store() first")
    return _store


# ---------------------------------------------------------------------------
# Middleware factory.
# ---------------------------------------------------------------------------

def build_middleware(
    catalog_provider: Callable[[], ProfileCatalog],
    store: SessionStore,
) -> Callable[..., dict[str, Any]]:
    """Return an ``llm_request`` middleware that injects when the session is enabled."""

    def middleware(request: dict[str, Any], **context: Any) -> dict[str, Any]:
        session_id = str(context.get("session_id") or "")
        if not session_id:
            return {"request": request}
        state = store.get(session_id)
        if not state.enabled or not state.profile_ids:
            return {"request": request}
        catalog = catalog_provider()
        if not catalog.available:
            return {"request": request}
        # Filter to known profile ids only.
        known = {p.id for p in catalog.profiles}
        ids = [pid for pid in state.profile_ids if pid in known]
        if not ids:
            return {"request": request}
        payload = assemble_payload(catalog, ids)
        if not payload:
            return {"request": request}
        new_request = inject_into_request(request, payload)
        if new_request is request:
            return {"request": request}
        return {"request": new_request, "source": "hermes_app.profiles", "reason": "session_enabled"}

    return middleware
