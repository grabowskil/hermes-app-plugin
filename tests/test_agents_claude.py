import json
import os

import pytest

from hermes_app.agents import claude_code as cc

CWD = "/work/sample-app/.claude/worktrees/bridge-cse_ABC123"


def _line(t, content, ts, **extra):
    msg = {"role": t, "content": content}
    msg.update(extra.pop("message", {}))
    d = {"type": t, "timestamp": ts, "cwd": CWD, "gitBranch": "feat/x", "message": msg}
    d.update(extra)
    return json.dumps(d)


@pytest.fixture
def host(tmp_path, monkeypatch):
    cfg = tmp_path / "claude"
    proj = cfg / "projects" / "-work-sample-app"
    proj.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cc, "_agents_list", lambda: [])
    monkeypatch.setattr(cc, "_ps", lambda: "")
    monkeypatch.setattr(cc, "binary", lambda: "/bin/claude")
    cc.invalidate()
    lines = [
        json.dumps({"type": "queue-operation"}),
        _line("user", "<command-name>/x</command-name>", "2026-11-20T10:00:00Z"),
        _line("user", "first prompt\nsecond line", "2026-11-20T10:00:01Z"),
        _line("assistant", [{"type": "thinking", "thinking": ""}], "2026-11-20T10:00:02Z",
              message={"id": "m1", "model": "claude-opus-5-5"}),
        _line("assistant", [{"type": "text", "text": "Looking."}], "2026-11-20T10:00:03Z", message={"id": "m1"}),
        _line("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": 1}}],
              "2026-11-20T10:00:04Z", message={"id": "m1"}),
        _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "x" * 3000}]}],
              "2026-11-20T10:00:05Z"),
        _line("user", "meta", "2026-11-20T10:00:05Z", isMeta=True),
        _line("assistant", [{"type": "text", "text": "Done."}], "2026-11-20T10:00:06Z", message={"id": "m2"}),
        _line("user", [{"type": "text", "text": "thanks"}, {"type": "image"}], "2026-11-20T10:00:07Z"),
    ]
    (proj / "sid-1.jsonl").write_text("\n".join(lines) + "\n")
    (proj / "empty.jsonl").write_text(json.dumps({"type": "queue-operation"}) + "\n")
    return proj


def test_messages_grouping(host):
    msgs = cc.messages("sid-1")
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant", "user"]
    assert [m["id"] for m in msgs] == [3, 4, 7, 9, 10]
    a = msgs[1]
    assert a["content"] == "Looking."
    assert a["tool_calls"][0]["function"] == {"name": "Read", "arguments": '{"p": 1}'}
    assert msgs[2]["tool_name"] == "Read" and msgs[2]["tool_call_id"] == "t1"
    assert len(msgs[2]["content"]) == 2000
    assert msgs[4]["content"] == "thanks\n\n[image]"


def test_paging(host):
    assert [m["id"] for m in cc.messages("sid-1", limit=2)] == [9, 10]
    assert [m["id"] for m in cc.messages("sid-1", limit=2, offset=2)] == [4, 7]
    after = cc.messages_after("sid-1", 4)
    assert [m["id"] for m in after["data"]] == [7, 9, 10] and after["last_id"] == 10
    assert cc.messages_after("sid-1", 10) == {"data": [], "last_id": None}


def test_session_summary(host):
    lst = cc.sessions()
    assert [s["id"] for s in lst] == ["sid-1"]
    s = lst[0]
    assert s["preview"] == "first prompt second line"
    assert s["message_count"] == 4  # 2 prompts + 2 assistant messages
    assert s["model"] == "claude-opus-5-5" and s["branch"] == "feat/x"
    assert s["environment"] == "sample-app"
    assert s["open_url"] == "https://claude.ai/code/session_ABC123"
    assert s["live"] is False and s["options"] == {"model": "claude-opus-5-5"}
    assert cc.activity("sid-1")["active"] is False


def test_live(host, monkeypatch):
    monkeypatch.setattr(cc, "_agents_list", lambda: [{"pid": 42, "sessionId": "sid-1", "status": "busy"}])
    cc.invalidate()
    s = cc.session("sid-1")
    assert s["live"] and s["busy"] and s["holder_pid"] == 42
    assert cc.locate("sid-1")["busy"] is True
    assert cc.activity("sid-1")["kind"] == "background"


def test_drafts(host, tmp_path, monkeypatch):
    d = tmp_path / "repo"
    (d / ".git").mkdir(parents=True)
    monkeypatch.setenv("HERMES_APP_AGENT_DIRS", str(d))
    cc.invalidate()
    env = cc.environments()[0]
    assert env["name"] == "repo" and env["git"] and not env["remote_control"]
    s = cc.create_draft(env["id"], {"model": "claude-haiku-5-5", "bogus": 1})
    assert s["title"] == "New session" and s["message_count"] == 0
    assert s["options"]["model"] == "claude-haiku-5-5" and "bogus" not in s["options"]
    assert s["id"] in [x["id"] for x in cc.sessions()]
    s2 = cc.set_options(s["id"], {"effort": "max", "junk": 2})
    assert s2["options"]["effort"] == "max"
    assert cc.get_options(s["id"])["permission_mode"] == "acceptEdits"
    f = cc.fork_draft("sid-1")
    assert f["cwd"] == CWD and cc.draft(f["id"])["fork_of"] == "sid-1"
    cc.drop_draft(s["id"])
    assert cc.draft(s["id"]) is None
    with pytest.raises(KeyError):
        cc.create_draft("nope")
