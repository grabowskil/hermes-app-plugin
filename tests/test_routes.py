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


SECRET = "s3cr3t-cookie-value"
VIEW = {"id": "yt", "name": "YouTube", "login_url": "https://accounts.example.com/", "check_domain": "example.com",
        "status": "ok", "checked_at": None, "saved_at": None, "detail": None}


class FakeSites:
    """Stands in for ``hermes_app.sites`` (written by another slice)."""
    def __init__(self):
        self.saved = []
        self.started = []

    def list_sites(self):
        return [VIEW]

    def _known(self, site_id):
        if site_id != "yt":
            raise KeyError(site_id)

    def save_session(self, site_id, cookies, user_agent):
        self._known(site_id)
        if not cookies:
            raise ValueError("bad")
        self.saved.append((site_id, cookies, user_agent))

    def check(self, site_id):
        self._known(site_id)
        return VIEW

    async def keepalive(self, publish):
        self.started.append(publish)
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def fake_sites(monkeypatch):
    fake = FakeSites()
    for name in ("list_sites", "save_session", "check", "keepalive"):
        monkeypatch.setattr(routes.sites, name, getattr(fake, name), raising=False)
    return fake


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
                             ("POST", "/upload"), ("GET", "/search?q=hello"), ("GET", "/todos?session_id=s"), ("GET", "/sites"),
                             ("PUT", "/sites/yt/session"), ("POST", "/sites/yt/check")):
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

    assert _run(go) == (200, {"plugin": "hermes_app", "version": "2.1.0",
                              "features": ["notices", "devices", "upload", "search", "todos", "sites"]})


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


def test_sites_list_and_check(fake_sites):
    async def go(c):
        listed = await c.get("/app/v1/sites", headers=AUTH)
        checked = await c.post("/app/v1/sites/yt/check", headers=AUTH)
        unknown = await c.post("/app/v1/sites/nope/check", headers=AUTH)
        return listed.status, await listed.json(), checked.status, await checked.json(), unknown.status

    assert _run(go) == (200, {"sites": [VIEW]}, 200, VIEW, 404)


def test_sites_put_session(fake_sites):
    cookie = {"name": "SID", "value": SECRET, "domain": ".example.com", "path": "/", "expires": None,
              "secure": True, "http_only": True}

    async def go(c):
        ok = await c.put("/app/v1/sites/yt/session", json={"cookies": [cookie], "user_agent": "UA"}, headers=AUTH)
        ok_text = await ok.text()
        unknown = await c.put("/app/v1/sites/nope/session", json={"cookies": [cookie], "user_agent": "UA"}, headers=AUTH)
        empty = await c.put("/app/v1/sites/yt/session", json={"cookies": [], "user_agent": "UA"}, headers=AUTH)
        no_body = await c.put("/app/v1/sites/yt/session", data="nope", headers=AUTH)
        wrong_type = await c.put("/app/v1/sites/yt/session", json={"cookies": "x", "user_agent": "UA"}, headers=AUTH)
        texts = [ok_text] + [await r.text() for r in (unknown, empty, no_body, wrong_type)]
        return ok.status, unknown.status, empty.status, no_body.status, wrong_type.status, texts

    ok, unknown, empty, no_body, wrong_type, texts = _run(go)
    assert (ok, unknown, empty, no_body, wrong_type) == (200, 404, 400, 400, 400)
    assert json.loads(texts[0]) == VIEW
    assert fake_sites.saved == [("yt", [cookie], "UA")]
    assert all(SECRET not in t for t in texts)


def test_keepalive_starts_once_per_app_and_stops(fake_sites):
    async def main():
        app = web.Application()
        routes.wire(app, FakeAdapter())
        routes.wire(app, FakeAdapter())
        async with TestClient(TestServer(app)):
            await asyncio.sleep(0)
            task = app[routes.KEEPALIVE_KEY]
            assert len(fake_sites.started) == 1 and fake_sites.started[0] is adapter.publish
            assert not task.done()
        assert task.cancelled()

    asyncio.run(main())


def test_publish_stores_and_emits(monkeypatch):
    events = []

    class Ctx:
        def emit(self, event, payload=None):
            events.append((event, payload))

    monkeypatch.setattr(adapter, "_ctx", Ctx())
    notice = store.classify("hi")
    adapter.publish(notice)
    assert events == [("notice", notice)]
    assert store.latest(1)[0]["id"] == notice["id"]
