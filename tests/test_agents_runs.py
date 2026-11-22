import asyncio
import json
import os
import stat
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_app import routes
from hermes_app.agents import claude_code, runs
from hermes_app.agents import claude_code_run as cr

KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}
SID = "11111111-2222-3333-4444-555555555555"


class FakeAdapter:
    _api_key = KEY


FAKE_CLAUDE = """#!{python}
import json, sys, time
mode = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else ""
prompt = sys.stdin.read()
def out(o):
    print(json.dumps(o), flush=True)
out({{"type": "system", "subtype": "init", "session_id": "{sid}"}})
if "hang" in prompt:
    time.sleep(60)
out({{"type": "stream_event", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": "Hel"}}}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Bash", "input": {{"command": "echo hi"}}}}]}}}})
out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "content": "hi", "is_error": False}}]}}}})
out({{"type": "result", "is_error": False, "result": "Hello " + prompt.strip()}})
"""


@pytest.fixture
def fake(monkeypatch, tmp_path):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, sid=SID))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    state = {"live": False, "invalidated": []}
    monkeypatch.setattr(claude_code, "binary", lambda: str(script))
    monkeypatch.setattr(claude_code, "locate", lambda sid: None if sid != SID else {
        "id": SID, "path": tmp_path / "t.jsonl", "cwd": str(tmp_path), "live": state["live"], "busy": False,
        "holder_pid": 99999999, "open_url": "https://example.com/code/x", "draft": None})
    monkeypatch.setattr(claude_code, "get_options", lambda sid: {"model": "m", "effort": "low", "permission_mode": "default"})
    monkeypatch.setattr(claude_code, "invalidate", lambda sid=None: state["invalidated"].append(sid), raising=False)
    monkeypatch.setattr(claude_code, "sessions", lambda limit=80: [
        {"id": SID, "provider": "claude_code", "title": "t", "live": False, "busy": False, "holder_pid": 4242}])
    monkeypatch.setattr(claude_code, "session", lambda sid: {"id": sid, "title": "t", "holder_pid": None})
    monkeypatch.setattr(claude_code, "activity", lambda sid: {"active": False})
    runs._runs.clear()
    return state


def with_client(fn):
    """Run ``async fn(client, fake)`` against the wired routes (no pytest-asyncio needed)."""
    def test(fake):
        async def main():
            app = web.Application()
            routes.wire(app, FakeAdapter())
            async with TestClient(TestServer(app)) as c:
                await fn(c, fake)
        asyncio.run(main())
    test.__name__ = fn.__name__
    return test


async def _events(client, run_id):
    resp = await client.get(f"/app/v1/agents/claude_code/runs/{run_id}/events", headers=AUTH)
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"
    text = await resp.text()
    return [json.loads(l[6:]) for l in text.splitlines() if l.startswith("data: ")]


def test_translate_mapping():
    st = {}
    assert cr.translate({"type": "system", "subtype": "init", "session_id": "s"}, st) == [{"event": "session.started", "session_id": "s"}]
    delta = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}}
    assert cr.translate(delta, st) == [{"event": "message.delta", "delta": "x"}]
    think = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x"}}}
    assert cr.translate(think, st) == []
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}}]}}
    assert cr.translate(use, st) == [{"event": "tool.started", "tool": "Read", "preview": "/x"}]
    res = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": "ok"}], "is_error": True}]}}
    (done,) = cr.translate(res, st)
    assert done["event"] == "tool.completed" and done["tool"] == "Read" and done["error"] is True and done["preview"] == "ok"
    assert cr.translate({"type": "result", "is_error": False, "result": "fin"}, st) == [{"event": "run.completed", "output": "fin"}]
    assert cr.translate({"type": "result", "is_error": True, "result": "boom"}, st) == [{"event": "run.failed", "error": "boom"}]
    assert cr.translate({"type": "rate_limit_event"}, st) == []


def test_command_shapes(tmp_path):
    opts = {"model": "m", "effort": "low", "permission_mode": "plan", "worktree": True}
    argv, cwd, stdin = cr.command("claude", "sid", "-hi", opts, {"cwd": str(tmp_path), "draft": None})
    assert argv[-2:] == ["--resume", "sid"] and stdin == "-hi" and cwd == str(tmp_path)
    (tmp_path / ".git").mkdir()
    argv, cwd, _ = cr.command("claude", "sid", "x", opts, {"path": None, "draft": {"cwd": str(tmp_path), "fork_of": None}})
    assert argv[-4:] == ["--session-id", "sid", "-w", "petasos-sid"]
    argv, _, _ = cr.command("claude", "sid", "x", opts, {"path": None, "draft": {"cwd": str(tmp_path), "fork_of": "src"}})
    assert argv[-5:] == ["--resume", "src", "--fork-session", "--session-id", "sid"]
    gone = str(tmp_path / ".claude/worktrees/bridge-1")
    _, cwd, _ = cr.command("claude", "sid", "x", opts, {"cwd": gone, "draft": None})
    assert cwd == str(tmp_path)


def test_child_env_scrubbed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = cr.child_env()
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env and "PATH" in env


@with_client
async def test_auth_required(client, fake):
    assert (await client.get("/app/v1/agents")).status == 401
    assert (await client.post(f"/app/v1/agents/claude_code/sessions/{SID}/runs", json={"input": "x"})).status == 401


@with_client
async def test_unknown_provider_and_lists(client, fake):
    resp = await client.get("/app/v1/agents/nope/sessions", headers=AUTH)
    assert resp.status == 404 and (await resp.json()) == {"error": "unknown provider"}
    data = (await (await client.get("/app/v1/agents/claude_code/sessions", headers=AUTH)).json())["data"]
    assert data[0]["id"] == SID and "holder_pid" not in data[0]


@with_client
async def test_run_lifecycle(client, fake):
    resp = await client.post(f"/app/v1/agents/claude_code/sessions/{SID}/runs", headers=AUTH, json={"input": "-world"})
    assert resp.status == 200
    body = await resp.json()
    assert body["session_id"] == SID and body["run_id"].startswith("arun_")
    events = await _events(client, body["run_id"])
    names = [e["event"] for e in events]
    assert names == ["session.started", "message.delta", "tool.started", "tool.completed", "run.completed"]
    assert events[-1]["output"] == "Hello -world"
    status = await (await client.get(f"/app/v1/agents/claude_code/runs/{body['run_id']}", headers=AUTH)).json()
    assert status == {"status": "completed", "output": "Hello -world"}
    # replay after the run finished
    assert [e["event"] for e in await _events(client, body["run_id"])] == names
    assert SID in fake["invalidated"]


@with_client
async def test_live_elsewhere_409(client, fake):
    fake["live"] = True
    resp = await client.post(f"/app/v1/agents/claude_code/sessions/{SID}/runs", headers=AUTH, json={"input": "x"})
    assert resp.status == 409
    assert await resp.json() == {"error": "live_elsewhere", "open_url": "https://example.com/code/x"}
    missing = await client.post("/app/v1/agents/claude_code/sessions/nope/runs", headers=AUTH, json={"input": "x"})
    assert missing.status == 404


@with_client
async def test_stop_cancels_and_busy(client, fake):
    url = f"/app/v1/agents/claude_code/sessions/{SID}/runs"
    run_id = (await (await client.post(url, headers=AUTH, json={"input": "hang"})).json())["run_id"]
    assert runs.active_run(SID) == run_id
    act = await (await client.get(f"/app/v1/agents/claude_code/sessions/{SID}/activity", headers=AUTH)).json()
    assert act["active"] is True and act["run_id"] == run_id
    assert (await client.post(url, headers=AUTH, json={"input": "again"})).status == 409
    stop = await client.post(f"/app/v1/agents/claude_code/runs/{run_id}/stop", headers=AUTH)
    assert (await stop.json()) == {"status": "stopping"}
    events = await _events(client, run_id)
    assert events[-1]["event"] == "run.cancelled"
    assert (await (await client.get(f"/app/v1/agents/claude_code/runs/{run_id}", headers=AUTH)).json())["status"] == "cancelled"
