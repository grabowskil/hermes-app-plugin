"""Claude Code as an agent provider: reads its local transcripts, lists host folders, keeps drafts."""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .. import store

ID = "claude_code"
_MAX_AGE = 30 * 86400
_TOOL_CUT = 2000
_SKIP_PREFIXES = ("<command-name>", "<local-command-stdout>", "<command-message>", "Caveat:")
_WORKTREE_TAIL = re.compile(r"/\.claude/worktrees/.*$")
_BRIDGE = re.compile(r"bridge-cse_([A-Za-z0-9]+)")
_DEFAULTS = {"model": "claude-opus-5-5", "effort": "high", "permission_mode": "acceptEdits", "worktree": True}
_OPTION_KEYS = tuple(_DEFAULTS)

_lock = threading.Lock()
_summary_cache: dict = {}
_msg_cache: dict = {}
_live_cache = {"t": 0.0, "data": {}}
_env_cache = {"t": 0.0, "data": []}


def describe() -> dict:
    return {
        "id": ID,
        "label": "Claude Code",
        "short": "Claude",
        "available": bool(binary()),
        "capabilities": ["environments", "model_switch", "effort", "permission_modes", "worktree",
                         "fork", "open_in_app", "live_follow"],
        "models": [
            {"id": "claude-opus-5-5", "label": "Opus 5.5", "detail": "Default · strongest everyday"},
            {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "detail": "Fast, capable"},
            {"id": "claude-haiku-5-5", "label": "Haiku 5.5", "detail": "Fastest, cheapest"},
            {"id": "claude-fable-5-2", "label": "Fable 5.2", "detail": "Most capable, slow"},
        ],
        "efforts": [{"id": e, "label": l} for e, l in
                    (("low", "LOW"), ("medium", "MED"), ("high", "HIGH"), ("xhigh", "XHIGH"), ("max", "MAX"))],
        "permission_modes": [
            {"id": "acceptEdits", "label": "Accept edits", "detail": "Edits run, other tools ask and are refused"},
            {"id": "auto", "label": "Auto", "detail": "A classifier allows safe actions"},
            {"id": "plan", "label": "Plan", "detail": "Reads and plans, changes nothing"},
            {"id": "bypassPermissions", "label": "Bypass", "detail": "Everything runs. Use with care"},
        ],
        "defaults": dict(_DEFAULTS),
    }


def binary():
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "claude", Path("/opt/homebrew/bin/claude")):
        if p.exists():
            return str(p)
    return None


def _config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def _run(cmd, timeout=4) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ---------- running processes ----------

def _agents_list() -> list:
    exe = binary()
    if not exe:
        return []
    try:
        data = json.loads(_run([exe, "agents", "--json"], timeout=6) or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _bridge_id(pid) -> str | None:
    try:
        d = json.loads((_config_dir() / "sessions" / f"{pid}.json").read_text())
        b = d.get("bridgeSessionId")
        return b if isinstance(b, str) and b else None
    except Exception:
        return None


def _live() -> dict:
    """sessionId -> {pid, busy, bridge}, cached for a few seconds."""
    now = time.time()
    with _lock:
        if now - _live_cache["t"] < 5:
            return _live_cache["data"]
    out = {}
    for a in _agents_list():
        sid = a.get("sessionId")
        if sid:
            out[sid] = {"pid": a.get("pid"), "busy": a.get("status") == "busy", "bridge": _bridge_id(a.get("pid"))}
    with _lock:
        _live_cache.update(t=now, data=out)
    return out


def _ps() -> str:
    return _run(["ps", "-axo", "pid,command"])


def _pid_cwd(pid) -> str | None:
    for line in _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _remote_servers() -> list:
    out = []
    for line in _ps().splitlines():
        line = line.strip()
        if "remote-control" not in line or "--sdk-url" in line:
            continue
        pid, _, cmd = line.partition(" ")
        try:
            argv = shlex.split(cmd)
        except ValueError:
            argv = cmd.split()
        if "remote-control" not in argv or "claude" not in " ".join(argv[:3]):
            continue
        name = None
        for i, a in enumerate(argv):
            if a == "--name" and i + 1 < len(argv):
                name = argv[i + 1]
            elif a.startswith("--name="):
                name = a.split("=", 1)[1]
        path = _pid_cwd(pid)
        if path:
            out.append({"path": path, "name": name})
    return out


def _env(path: str, name: str | None, rc: bool) -> dict:
    return {"id": hashlib.sha1(path.encode()).hexdigest()[:12], "name": name or os.path.basename(path.rstrip("/")) or path,
            "path": path, "remote_control": rc, "git": os.path.exists(os.path.join(path, ".git"))}


def environments() -> list:
    now = time.time()
    with _lock:
        if now - _env_cache["t"] < 30 and _env_cache["data"]:
            return _env_cache["data"]
    found = {}
    for s in _remote_servers():
        found[s["path"]] = _env(s["path"], s["name"], True)
    for p in os.environ.get("HERMES_APP_AGENT_DIRS", "").split(os.pathsep):
        p = p.strip()
        if p and p not in found and os.path.isdir(p):
            found[p] = _env(p, None, False)
    data = sorted(found.values(), key=lambda e: e["name"].lower())
    with _lock:
        _env_cache.update(t=now, data=data)
    return data


def environment(env_id):
    return next((e for e in environments() if e["id"] == env_id), None)


# ---------- transcripts ----------

def _projects() -> Path:
    return _config_dir() / "projects"


def _transcripts() -> list:
    try:
        return list(_projects().glob("*/*.jsonl"))
    except OSError:
        return []


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _prompt_text(content) -> str | None:
    """The typed prompt of a user line, or None when it is a tool result or system text."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[image]")
    else:
        return None
    kept = []
    for p in parts:
        s = p.strip()
        if not s or s.startswith(_SKIP_PREFIXES) or (s.startswith("<system-reminder>") and s.endswith("</system-reminder>")):
            continue
        kept.append(p)
    text = "\n\n".join(kept).strip()
    return text or None


def _result_text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content or "")[:_TOOL_CUT]


def _parse(path: Path) -> tuple:
    """Return (summary, messages) for one transcript."""
    messages: list = []
    tool_names: dict = {}
    s = {"cwd": None, "branch": None, "model": None, "first": None, "first_ts": None, "last_ts": None, "prompts": 0}
    cur = None  # current assistant message being grouped
    cur_mid = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for n, raw in enumerate(f, 1):
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            t = d.get("type")
            if t not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
                continue
            msg = d.get("message") or {}
            ts = _ts(d.get("timestamp"))
            if d.get("cwd"):
                s["cwd"] = d["cwd"]
            if d.get("gitBranch"):
                s["branch"] = d["gitBranch"]
            content = msg.get("content")
            if t == "assistant":
                if msg.get("model") and not str(msg["model"]).startswith("<"):
                    s["model"] = msg["model"]
                blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
                mid = msg.get("id")
                if cur is None or mid is None or mid != cur_mid:
                    cur = {"id": n, "role": "assistant", "content": "", "tool_calls": None, "tool_call_id": None,
                           "tool_name": None, "timestamp": ts, "reasoning": None, "_texts": [], "_think": []}
                    cur_mid = mid
                    messages.append(cur)
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and (b.get("text") or "").strip():
                        cur["_texts"].append(b["text"])
                    elif bt == "thinking" and (b.get("thinking") or "").strip():
                        cur["_think"].append(b["thinking"])
                    elif bt == "tool_use":
                        tool_names[b.get("id")] = b.get("name")
                        cur["tool_calls"] = (cur["tool_calls"] or []) + [{
                            "id": b.get("id"), "type": "function",
                            "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}]
                cur["content"] = "\n\n".join(cur["_texts"])
                cur["reasoning"] = "\n\n".join(cur["_think"]) or None
                s["last_ts"] = ts or s["last_ts"]
                continue
            # user line
            cur, cur_mid = None, None
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        messages.append({"id": n, "role": "tool", "content": _result_text(b.get("content")),
                                         "tool_calls": None, "tool_call_id": b.get("tool_use_id"),
                                         "tool_name": tool_names.get(b.get("tool_use_id")), "timestamp": ts,
                                         "reasoning": None})
                s["last_ts"] = ts or s["last_ts"]
                continue
            text = _prompt_text(content)
            if text is None:
                continue
            s["prompts"] += 1
            if s["first"] is None:
                s["first"], s["first_ts"] = text, ts
            s["last_ts"] = ts or s["last_ts"]
            messages.append({"id": n, "role": "user", "content": text, "tool_calls": None, "tool_call_id": None,
                             "tool_name": None, "timestamp": ts, "reasoning": None})
    for m in messages:
        m.pop("_texts", None)
        m.pop("_think", None)
    s["count"] = s["prompts"] + sum(1 for m in messages if m["role"] == "assistant")
    return s, messages


def _stat_key(path: Path):
    st = path.stat()
    return (str(path), st.st_mtime, st.st_size), st


def _load(path: Path) -> tuple:
    key, st = _stat_key(path)
    with _lock:
        hit = _msg_cache.get(key)
        if hit:
            return hit
    parsed = _parse(path)
    with _lock:
        _summary_cache[str(path)] = (key, parsed[0])
        _msg_cache.clear() if len(_msg_cache) > 6 else None
        _msg_cache[key] = parsed
    return parsed


def _summary(path: Path) -> dict:
    key, _ = _stat_key(path)
    with _lock:
        hit = _summary_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    return _load(path)[0]


def _repo_root(cwd: str) -> str:
    return _WORKTREE_TAIL.sub("", cwd or "").rstrip("/")


def _env_name(cwd: str) -> str | None:
    root = _repo_root(cwd)
    if not root:
        return None
    for e in environments():
        if e["path"].rstrip("/") == root:
            return e["name"]
    return os.path.basename(root) or None


def _open_url(cwd: str, live_info: dict | None) -> str | None:
    bridge = (live_info or {}).get("bridge")
    if bridge:
        return f"https://claude.ai/code/{bridge}"
    m = _BRIDGE.search(cwd or "")
    return f"https://claude.ai/code/session_{m.group(1)}" if m else None


# ---------- drafts and options ----------

def _store_path() -> Path:
    return store._data_dir() / "agents.json"


def _read_store() -> dict:
    try:
        d = json.loads(_store_path().read_text())
        if isinstance(d, dict):
            d.setdefault("drafts", {})
            d.setdefault("options", {})
            return d
    except Exception:
        pass
    return {"drafts": {}, "options": {}}


def _write_store(d: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    os.replace(tmp, p)


def _clean_options(options) -> dict:
    return {k: v for k, v in (options or {}).items() if k in _OPTION_KEYS and v is not None}


def draft(sid):
    return _read_store()["drafts"].get(sid)


def drop_draft(sid) -> None:
    with _lock:
        d = _read_store()
        if d["drafts"].pop(sid, None) is not None:
            _write_store(d)


def get_options(sid) -> dict:
    d = _read_store()
    return {**_DEFAULTS, **d["options"].get(sid, {})}


def set_options(sid, options) -> dict:
    with _lock:
        d = _read_store()
        clean = _clean_options(options)
        if sid in d["drafts"]:
            d["drafts"][sid]["options"] = {**d["drafts"][sid].get("options", {}), **clean}
        d["options"][sid] = {**d["options"].get(sid, {}), **clean}
        _write_store(d)
    return session(sid)


def create_draft(env_id, options=None) -> dict:
    env = environment(env_id)
    if not env:
        raise KeyError(env_id)
    sid = str(uuid.uuid4())
    opts = {**_DEFAULTS, **_clean_options(options)}
    with _lock:
        d = _read_store()
        d["drafts"][sid] = {"id": sid, "cwd": env["path"], "env_id": env_id, "options": opts,
                            "fork_of": None, "created_at": time.time()}
        d["options"][sid] = dict(opts)
        _write_store(d)
    return session(sid)


def fork_draft(src_sid) -> dict:
    src = locate(src_sid)
    if not src or not src.get("cwd"):
        raise KeyError(src_sid)
    sid = str(uuid.uuid4())
    opts = get_options(src_sid)
    root = _repo_root(src["cwd"])
    env = next((e for e in environments() if e["path"].rstrip("/") == root), None)
    with _lock:
        d = _read_store()
        d["drafts"][sid] = {"id": sid, "cwd": src["cwd"], "env_id": env["id"] if env else None, "options": opts,
                            "fork_of": src_sid, "created_at": time.time()}
        d["options"][sid] = dict(opts)
        _write_store(d)
    return session(sid)


# ---------- sessions ----------

def _find(sid) -> Path | None:
    if not re.fullmatch(r"[0-9A-Za-z-]+", sid or ""):
        return None
    for p in _projects().glob(f"*/{sid}.jsonl"):
        return p
    return None


def _holder(sid) -> dict:
    return _live().get(sid) or {}


def locate(sid):
    path = _find(sid)
    dr = draft(sid)
    if not path and not dr:
        return None
    cwd = dr["cwd"] if dr else None
    if path:
        try:
            cwd = _summary(path)["cwd"] or cwd
        except OSError:
            pass
    h = _holder(sid)
    return {"id": sid, "path": path, "cwd": cwd, "live": bool(h), "busy": bool(h.get("busy")),
            "holder_pid": h.get("pid"), "open_url": _open_url(cwd or "", h), "draft": dr}


def _oneline(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= 120 else one[:119] + "…"


def _session_from(path: Path, sid: str, dr) -> dict:
    s = _summary(path)
    cwd = s["cwd"] or (dr or {}).get("cwd") or ""
    h = _holder(sid)
    stored = _read_store()["options"].get(sid)
    return {"id": sid, "provider": ID, "source": ID, "title": None,
            "preview": _oneline(s["first"] or ""), "model": s["model"],
            "started_at": s["first_ts"], "last_active": s["last_ts"] or path.stat().st_mtime,
            "message_count": s["count"], "environment": _env_name(cwd), "branch": s["branch"],
            "live": bool(h), "busy": bool(h.get("busy")), "holder_pid": h.get("pid"),
            "open_url": _open_url(cwd, h), "options": stored or {"model": s["model"]}, "cwd": cwd}


def _draft_session(dr: dict) -> dict:
    return {"id": dr["id"], "provider": ID, "source": ID, "title": "New session", "preview": "",
            "model": dr["options"].get("model"), "started_at": dr["created_at"], "last_active": dr["created_at"],
            "message_count": 0, "environment": _env_name(dr["cwd"]), "branch": None, "live": False,
            "busy": False, "holder_pid": None, "open_url": None, "options": dr["options"], "cwd": dr["cwd"]}


def session(sid):
    path = _find(sid)
    if path:
        try:
            if _summary(path)["prompts"] or not draft(sid):
                return _session_from(path, sid, draft(sid))
        except OSError:
            pass
    dr = draft(sid)
    return _draft_session(dr) if dr else None


def sessions(limit=80) -> list:
    cutoff = time.time() - _MAX_AGE
    files = []
    for p in _transcripts():
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt >= cutoff:
            files.append((mt, p))
    files.sort(key=lambda x: x[0], reverse=True)
    drafts = _read_store()["drafts"]
    out = []
    for _, p in files:
        try:
            if not _summary(p)["prompts"]:
                continue
            out.append(_session_from(p, p.stem, drafts.get(p.stem)))
        except OSError:
            continue
        if len(out) >= limit * 2:
            break
    have = {s["id"] for s in out}
    out += [_draft_session(dr) for sid, dr in drafts.items() if sid not in have]
    out.sort(key=lambda s: s["last_active"] or 0, reverse=True)
    return out[:limit]


def messages(sid, limit=60, offset=0) -> list:
    path = _find(sid)
    if not path:
        return []
    msgs = _load(path)[1]
    end = len(msgs) - max(offset, 0)
    return msgs[max(end - limit, 0):max(end, 0)]


def messages_after(sid, after_id, limit=200) -> dict:
    path = _find(sid)
    if not path:
        return {"data": [], "last_id": None}
    data = [m for m in _load(path)[1] if m["id"] > after_id][:limit]
    return {"data": data, "last_id": data[-1]["id"] if data else None}


def activity(sid) -> dict:
    s = session(sid) or {}
    busy = bool(s.get("busy"))
    return {"active": busy, "kind": "background" if busy else None, "run_id": None, "api_calls": 0,
            "tool_calls": 0, "tokens_in": 0, "tokens_out": 0, "started_at": None,
            "updated_at": s.get("last_active"), "mode": None}


def invalidate(sid=None) -> None:
    with _lock:
        _live_cache["t"] = 0.0
        _env_cache["t"] = 0.0
        if sid is None:
            _summary_cache.clear()
            _msg_cache.clear()
        else:
            for k in [k for k in _summary_cache if k.endswith(f"{sid}.jsonl")]:
                _summary_cache.pop(k, None)
            for k in [k for k in _msg_cache if k[0].endswith(f"{sid}.jsonl")]:
                _msg_cache.pop(k, None)


try:
    from .claude_code_run import command, translate  # noqa: E402,F401  (slice S2)
except ImportError:
    pass
