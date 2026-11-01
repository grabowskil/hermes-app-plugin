"""``/stats``, ``/files`` and ``/share/actions``."""
import asyncio
import sqlite3
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_app import routes

KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}


class FakeAdapter:
    _api_key = KEY


def _run(coro):
    async def main():
        app = web.Application()
        routes.wire(app, FakeAdapter())
        async with TestClient(TestServer(app)) as c:
            return await coro(c)

    return asyncio.run(main())


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path / "app"))
    return tmp_path


def _db(home, texts):
    now = time.time()
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                " content TEXT, timestamp REAL, active INTEGER DEFAULT 1)")
    con.executemany("INSERT INTO sessions VALUES (?, ?)", [("s1", now), ("s2", now - 30 * 86400)])
    rows = [("s1", "assistant", t, now) for t in texts]
    rows += [("s1", "user", "MEDIA:/tmp/user-said.txt", now), ("s1", "tool", "x", now), ("s1", "tool", "y", now),
             ("s2", "assistant", "old", now - 30 * 86400)]
    con.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()


def test_stats_shape(home):
    _db(home, ["hi"])

    async def go(c):
        return await (await c.get("/app/v1/stats", headers=AUTH)).json()

    assert _run(go) == {"sessions_7d": 1, "messages_7d": 4, "tool_calls_7d": 2, "sessions_total": 2}


def test_stats_without_db_is_zero(home):
    async def go(c):
        return await (await c.get("/app/v1/stats", headers=AUTH)).json()

    assert _run(go) == {"sessions_7d": 0, "messages_7d": 0, "tool_calls_7d": 0, "sessions_total": 0}


def test_files(home):
    report = home / "report.pdf"
    report.write_bytes(b"%PDF-1.4")
    _db(home, [f"Done.\nMEDIA: {report}\nBye", "MEDIA:/tmp/gone.txt", f"MEDIA:{home}/rep"])
    (home / "user-said.txt").write_text("x")

    async def get(c, session, path):
        r = await c.get("/app/v1/files", params={"session": session, "path": path}, headers=AUTH)
        return r.status, r

    async def go(c):
        ok_status, ok = await get(c, "s1", str(report))
        return (
            (ok_status, ok.headers["Content-Type"], ok.headers["Content-Disposition"], await ok.read()),
            (await get(c, "s1", "report.pdf"))[0],                   # relative
            (await get(c, "s2", str(report)))[0],                    # other session
            (await get(c, "s1", str(home / "other.txt")))[0],        # never named
            (await get(c, "s1", "/tmp/user-said.txt"))[0],           # named by the user only
            (await get(c, "s1", "/tmp/gone.txt"))[0],                # named, not on disk
            (await get(c, "s1", f"{home}/rep"))[0],                  # prefix of a named path is its own entry
            (await get(c, "s1", f"{home}/re"))[0],                   # a prefix of a named path is not named
        )

    ok, relative, other, unnamed, user_only, gone, rep, prefix = _run(go)
    assert ok == (200, "application/pdf", 'attachment; filename="report.pdf"', b"%PDF-1.4")
    assert (relative, other, unnamed, user_only, gone, prefix) == (400, 403, 403, 403, 404, 403)
    assert rep == 404  # named, but no such file


def test_share_actions_missing_file(home):
    async def go(c):
        return await (await c.get("/app/v1/share/actions", headers=AUTH)).json()

    assert _run(go) == {"decide_prompt": None, "actions": []}


def test_share_actions_parsing(home):
    folder = home / "hermes_app"
    folder.mkdir()
    (folder / "share_actions.yaml").write_text(
        'decide_prompt: "Decide: {item}"\n'
        "actions:\n"
        "  - {id: summarize, label: Summarize, prompt: 'Summarize {item}', match: url}\n"
        "  - {id: note, label: Note, prompt: 'Note {item}'}\n"
        "  - {id: bad-match, label: Bad, prompt: 'x', match: video}\n"
        "  - {id: no-prompt, label: Bad}\n"
        "  - just text\n",
        encoding="utf-8",
    )

    async def go(c):
        return await (await c.get("/app/v1/share/actions", headers=AUTH)).json()

    assert _run(go) == {
        "decide_prompt": "Decide: {item}",
        "actions": [
            {"id": "summarize", "label": "Summarize", "prompt": "Summarize {item}", "match": "url"},
            {"id": "note", "label": "Note", "prompt": "Note {item}", "match": "any"},
        ],
    }
