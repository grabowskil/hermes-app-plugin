import asyncio
import json
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_app import routes, transcript

KEY = "k" * 32


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
        " tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, token_count INTEGER,"
        " finish_reason TEXT, reasoning TEXT, reasoning_content TEXT, display_kind TEXT, active INTEGER DEFAULT 1)"
    )
    rows = [
        ("s1", "user", "hi", None, None, None, 1.0, None, None, None, None, None, 1),
        ("s1", "assistant", "", None, json.dumps([{"id": "c1"}]), None, 2.0, None, "tool_calls", None, None, None, 1),
        ("s1", "tool", "x" * 3000, "c1", None, "web_search", 3.0, None, None, None, None, None, 1),
        ("s1", "tool", "ok", "c1", None, "web_search", 4.0, None, None, None, None, None, 1),
        ("s1", "assistant", "old", None, None, None, 5.0, None, None, None, None, None, 0),
        ("s2", "user", "other", None, None, None, 6.0, None, None, None, None, None, 1),
    ]
    con.executemany(
        "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,"
        " token_count, finish_reason, reasoning, reasoning_content, display_kind, active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def test_pages_after_id_and_trims_tool_output(db):
    page = transcript.messages_after("s1", 0, 200)
    assert [m["id"] for m in page["data"]] == [1, 2, 3, 4] and page["last_id"] == 4
    assert page["data"][1]["tool_calls"] == [{"id": "c1"}]
    big, small = page["data"][2], page["data"][3]
    assert len(big["content"]) == transcript.TOOL_CHARS and big["truncated"] is True
    assert "truncated" not in small and small["content"] == "ok"
    assert set(small) >= {"id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp"}
    assert transcript.messages_after("s1", 2, 1) == {"data": [page["data"][2]], "last_id": 3}
    assert transcript.messages_after("s1", 4) == {"data": [], "last_id": 4}


def test_missing_db_gives_empty_page(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))
    assert transcript.messages_after("s1", 7) == {"data": [], "last_id": 7}


def test_messages_route(db):
    class A:
        _api_key = KEY

    async def main():
        app = web.Application()
        routes.wire(app, A())
        async with TestClient(TestServer(app)) as c:
            h = {"Authorization": f"Bearer {KEY}"}
            ok = await c.get("/app/v1/sessions/s1/messages?after_id=1&limit=1", headers=h)
            bad = await c.get("/app/v1/sessions/s1/messages?after_id=x", headers=h)
            return (await ok.json())["last_id"], bad.status

    assert asyncio.run(main()) == (2, 400)
