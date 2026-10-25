import asyncio
import json
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_app import adapter, routes, store, todos

KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}


class FakeAdapter:
    """No ``_check_auth``: exercises the bearer-check fallback."""
    _api_key = KEY


class CoreLikeAdapter:
    """Has the API server's ``_check_auth`` contract: None when OK, else a 401 response."""
    def _check_auth(self, request):
        if request.headers.get("Authorization") == AUTH["Authorization"]:
            return None
        return web.json_response({"error": "core"}, status=401)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    todos._latest.clear()


def _run(coro_fn, adapter_obj=None):
    async def main():
        app = web.Application()
        routes.wire(app, adapter_obj or FakeAdapter())
        async with TestClient(TestServer(app)) as client:
            return await coro_fn(client)

    return asyncio.run(main())


def test_every_route_needs_the_key():
    async def go(c):
        out = []
        for method, path in (("GET", "/capabilities"), ("GET", "/notices"), ("POST", "/devices"),
                             ("POST", "/upload"), ("GET", "/search?q=hello"), ("GET", "/todos?session_id=s")):
            out.append((await c.request(method, "/app/v1" + path)).status)
            out.append((await c.request(method, "/app/v1" + path, headers={"Authorization": "Bearer nope"})).status)
        return out

    assert set(_run(go)) == {401}


def test_core_check_auth_is_used_when_present():
    async def go(c):
        bad = await c.get("/app/v1/capabilities")
        good = await c.get("/app/v1/capabilities", headers=AUTH)
        return bad.status, (await bad.json()), good.status

    assert _run(go, CoreLikeAdapter()) == (401, {"error": "core"}, 200)


def test_no_key_configured_refuses():
    class NoKey:
        _api_key = ""

    async def go(c):
        return (await c.get("/app/v1/capabilities")).status

    assert _run(go, NoKey()) == 401


def test_capabilities():
    async def go(c):
        r = await c.get("/app/v1/capabilities", headers=AUTH)
        return r.status, await r.json()

    assert _run(go) == (200, {"plugin": "hermes_app", "version": "2.0.0",
                              "features": ["notices", "devices", "upload", "search", "todos"]})


def test_version_matches_manifest():
    text = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
    assert re.search(rf"^version: {re.escape(routes.VERSION)}$", text, re.M)


def test_notices_newest_first_and_limit():
    ids = []
    for text in ("one", "two", "three"):
        n = store.classify(text)
        store.append(n)
        ids.append(n["id"])

    async def go(c):
        all_ = await (await c.get("/app/v1/notices", headers=AUTH)).json()
        two = await (await c.get("/app/v1/notices?limit=2", headers=AUTH)).json()
        junk = await (await c.get("/app/v1/notices?limit=x", headers=AUTH)).json()
        return all_, two, junk

    all_, two, junk = _run(go)
    assert [n["id"] for n in all_["notices"]] == ids[::-1]
    assert [n["id"] for n in two["notices"]] == ids[::-1][:2]
    assert len(junk["notices"]) == 3


def test_devices_alert_and_widget():
    async def go(c):
        ok = await c.post("/app/v1/devices", json={"token": "aa", "environment": "sandbox"}, headers=AUTH)
        wid = await c.post("/app/v1/devices", json={"token": "bb", "kind": "widget"}, headers=AUTH)
        bad_env = await c.post("/app/v1/devices", json={"token": "cc", "environment": "x"}, headers=AUTH)
        bad_body = await c.post("/app/v1/devices", data="nope", headers=AUTH)
        return ok.status, wid.status, bad_env.status, bad_body.status

    assert _run(go) == (200, 200, 400, 400)
    assert store.load_devices() == [{"token": "aa", "environment": "sandbox"}]
    assert store.load_devices("widget") == [{"token": "bb", "environment": "production"}]


def test_upload_stores_file(tmp_path):
    async def go(c):
        r = await c.post("/app/v1/upload", data=b"hello", headers={**AUTH, "X-Filename": "a%20b.txt",
                                                                     "Content-Type": "text/plain"})
        empty = await c.post("/app/v1/upload", data=b"", headers=AUTH)
        return r.status, await r.json(), empty.status

    status, body, empty = _run(go)
    assert (status, empty) == (201, 400)
    assert body["size"] == 5 and body["name"].endswith("-a-b.txt")
    assert Path(body["path"]).read_bytes() == b"hello"
    assert str(tmp_path / "home" / "uploads") in body["path"]


def test_search_without_db_is_empty():
    async def go(c):
        return await (await c.get("/app/v1/search?q=hello", headers=AUTH)).json()

    assert _run(go) == {"hits": []}


def test_todos():
    todos.on_post_tool_call(tool_name="todo_list", session_id="s1",
                            result=json.dumps({"todos": [{"id": "1", "content": "x", "status": "pending"}]}))

    async def go(c):
        ok = await c.get("/app/v1/todos?session_id=s1", headers=AUTH)
        none = await c.get("/app/v1/todos?session_id=zz", headers=AUTH)
        missing = await c.get("/app/v1/todos", headers=AUTH)
        return ok.status, await ok.json(), none.status, missing.status

    status, body, none, missing = _run(go)
    assert status == 200 and body["todos"][0]["id"] == "1"
    assert (none, missing) == (404, 400)


def test_register_wires_api_server_handler():
    calls = []

    class Ctx:
        def register_platform_handler(self, platform, factory):
            calls.append((platform, factory))

    routes.register(Ctx())
    assert calls == [("api_server", routes.wire)]


def test_delivery_stores_and_emits(monkeypatch):
    events = []

    class Ctx:
        def emit(self, event, payload=None):
            events.append((event, payload))

    monkeypatch.setattr(adapter, "_ctx", Ctx())
    notice = adapter._deliver("hello")
    assert events == [("notice", notice)]
    assert store.latest(1)[0]["id"] == notice["id"]


def test_delivery_survives_emit_failure(monkeypatch):
    class Ctx:
        def emit(self, event, payload=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(adapter, "_ctx", Ctx())
    notice = adapter._deliver("hello")
    assert store.latest(1)[0]["id"] == notice["id"]
