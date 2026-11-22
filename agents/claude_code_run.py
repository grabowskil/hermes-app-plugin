"""Claude Code run pieces: the print-mode command line, stream-json -> app run events, child environment."""
import os
import re
import time
from typing import Optional

STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
             "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
_WORKTREE_TAIL = re.compile(r"/\.claude/worktrees/.*$")
_PREVIEW_KEYS = ("command", "file_path", "pattern", "url", "description", "prompt")


def child_env() -> dict:
    """The plugin's environment minus anything that would make Claude Code bill an API key."""
    env = dict(os.environ)
    for key in STRIP_ENV:
        env.pop(key, None)
    return env


def _is_git(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def _existing_cwd(cwd: str) -> str:
    """A worktree folder may be gone; fall back to the repository root it belonged to."""
    if cwd and os.path.isdir(cwd):
        return cwd
    root = _WORKTREE_TAIL.sub("", cwd or "")
    return root if root and os.path.isdir(root) else (cwd or os.getcwd())


def command(binary: str, sid: str, text: str, options: dict, located: dict) -> tuple:
    """(argv, cwd, stdin_text) for one print-mode run. The prompt goes on stdin so "-x" text is safe."""
    options = options or {}
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    for flag, key in (("--model", "model"), ("--effort", "effort"), ("--permission-mode", "permission_mode")):
        if options.get(key):
            argv += [flag, str(options[key])]
    draft = None if located.get("path") else located.get("draft")  # a transcript exists: resume it
    if not draft:
        argv += ["--resume", sid]
        return argv, _existing_cwd(located.get("cwd") or ""), text
    cwd = _existing_cwd(draft.get("cwd") or located.get("cwd") or "")
    if draft.get("fork_of"):
        argv += ["--resume", draft["fork_of"], "--fork-session", "--session-id", sid]
    else:
        argv += ["--session-id", sid]
        if options.get("worktree") and _is_git(cwd):
            argv += ["-w", "petasos-" + sid[:8]]
    return argv, cwd, text


def _preview(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in _PREVIEW_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value:
            return value[:200]
    return ""


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def translate(obj: dict, state: dict) -> list:
    """One stream-json object -> zero or more app run events. ``state`` maps tool id -> (name, start)."""
    kind = obj.get("type")
    out = []
    if kind == "stream_event":
        event = obj.get("event") or {}
        delta = event.get("delta") or {}
        if (event.get("type") == "content_block_delta" and delta.get("type") == "text_delta"
                and not obj.get("parent_tool_use_id") and delta.get("text")):
            out.append({"event": "message.delta", "delta": delta["text"]})
    elif kind == "assistant":
        content = (obj.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                state[block.get("id")] = (name, time.time())
                out.append({"event": "tool.started", "tool": name, "preview": _preview(block.get("input"))})
    elif kind == "user":
        content = (obj.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                name, start = state.pop(block.get("tool_use_id"), ("tool", time.time()))
                out.append({"event": "tool.completed", "tool": name, "duration": round(time.time() - start, 2),
                            "error": bool(block.get("is_error")), "preview": _result_text(block.get("content"))[:500]})
    elif kind == "system":
        if obj.get("subtype") == "init" and obj.get("session_id"):
            out.append({"event": "session.started", "session_id": obj["session_id"]})
    elif kind == "result":
        text = obj.get("result")
        if obj.get("is_error"):
            out.append({"event": "run.failed", "error": text if isinstance(text, str) and text else str(obj.get("subtype") or "error")})
        else:
            out.append({"event": "run.completed", "output": text if isinstance(text, str) else ""})
    return out
