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


OAUTH_VIEW = {**VIEW, "id": "oa", "kind": "oauth", "login_url": None, "redirect_uri": "http://localhost"}


class FakeSites:
    """Stands in for ``hermes_app.sites`` (written by another slice)."""
    def __init__(self):
        self.saved = []
        self.started = []
        self.checked = []
        self.keys = []
        self.views = {"yt": VIEW, "oa": OAUTH_VIEW, "ak": {**VIEW, "id": "ak", "kind": "api_key"}}

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
        self.checked.append(site_id)
        return VIEW

    def get(self, site_id):
        return self.views.get(site_id)

    def put(self, fields):
        if not fields.get("name"):
            raise ValueError("bad")
        return {**VIEW, "name": fields["name"]}

    def remove(self, site_id):
        return site_id == "yt"

    def set_api_key(self, site_id, key):
        if site_id != "yt":
            raise KeyError(site_id)
        if not key:
            raise ValueError("bad")
        self.keys.append((site_id, key))
        return VIEW

    async def keepalive(self, publish):
        self.started.append(publish)
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def fake_sites(monkeypatch):
    fake = FakeSites()
    for name in ("list_sites", "save_session", "check", "keepalive", "get", "put", "remove", "set_api_key"):
        monkeypatch.setattr(routes.sites, name, getattr(fake, name), raising=False)
    return fake


class FakeOAuth:
    def __init__(self):
        self.finished = []

    def start(self, site_id, redirect_uri=None):
        self.redirects = getattr(self, "redirects", []) + [redirect_uri]
        return {"auth_url": "https://auth.example.com/?state=x", "redirect_uri": redirect_uri or "http://localhost"}

    def finish(self, site_id, redirect_url):
        if "code=" not in redirect_url:
            raise ValueError("secret detail")
        self.finished.append((site_id, redirect_url))

    def matches_redirect(self, url, redirect_uri):
        return url.startswith(redirect_uri)


class FakeBrowser:
    def __init__(self, is_available=True):
        self.is_available = is_available
        self.started = []
        self.closed = []

    def available(self):
        return self.is_available

    async def start(self, site_id, url):
        self.started.append((site_id, url))

    async def cookies(self, site_id):
        return [{"name": "SID", "value": SECRET}]

    async def user_agent(self, site_id):
        return "UA"

    async def close(self, site_id):
        self.closed.append(site_id)

    async def close_all(self):
        self.closed.append("*")


@pytest.fixture(autouse=True)
def fake_oauth(monkeypatch):
    fake = FakeOAuth()
    monkeypatch.setattr(routes, "site_oauth", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_browser(monkeypatch):
    fake = FakeBrowser()
    monkeypatch.setattr(routes, "host_browser", fake)
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
                             ("PUT", "/sites/yt/session"), ("POST", "/sites/yt/check"), ("POST", "/sites"),
                             ("DELETE", "/sites/yt"), ("PUT", "/sites/yt/api-key"), ("POST", "/sites/yt/oauth/start"),
                             ("POST", "/sites/yt/oauth/finish"), ("POST", "/sites/yt/host-login"),
                             ("POST", "/sites/yt/host-login/finish")):
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


def test_capabilities(fake_browser, monkeypatch):
    monkeypatch.setattr(routes.voice, "features", lambda: [])

    async def go(c):
        r = await c.get("/app/v1/capabilities", headers=AUTH)
        return r.status, await r.json()

    base = ["notices", "devices", "upload", "search", "todos", "sites", "site_manage", "activity", "site_providers"]
    assert _run(go) == (200, {"plugin": "hermes_app", "version": "2.6.0", "features": base + ["host_login"]})
    fake_browser.is_available = False
    assert _run(go) == (200, {"plugin": "hermes_app", "version": "2.6.0", "features": base})


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


def test_site_manage_routes(fake_sites):
    async def go(c):
        add = await c.post("/app/v1/sites", json={"name": "Mine"}, headers=AUTH)
        add_bad = await c.post("/app/v1/sites", json={}, headers=AUTH)
        add_junk = await c.post("/app/v1/sites", data="nope", headers=AUTH)
        rm = await c.delete("/app/v1/sites/yt", headers=AUTH)
        rm_none = await c.delete("/app/v1/sites/nope", headers=AUTH)
        key = await c.put("/app/v1/sites/yt/api-key", json={"key": "K"}, headers=AUTH)
        key_none = await c.put("/app/v1/sites/nope/api-key", json={"key": "K"}, headers=AUTH)
        key_bad = await c.put("/app/v1/sites/yt/api-key", json={"key": ""}, headers=AUTH)
        key_missing = await c.put("/app/v1/sites/yt/api-key", json={}, headers=AUTH)
        return ((add.status, (await add.json())["name"]), add_bad.status, add_junk.status,
                (rm.status, await rm.json()), rm_none.status, key.status, key_none.status, key_bad.status,
                key_missing.status)

    assert _run(go) == ((200, "Mine"), 400, 400, (200, {"ok": True}), 404, 200, 404, 400, 400)
    assert fake_sites.keys == [("yt", "K")]


def test_oauth_start_and_finish(fake_oauth):
    async def go(c):
        start = await c.post("/app/v1/sites/oa/oauth/start", headers=AUTH)
        ok = await c.post("/app/v1/sites/oa/oauth/finish", json={"redirect_url": "http://localhost/?code=abc"},
                          headers=AUTH)
        bad = await c.post("/app/v1/sites/oa/oauth/finish", json={"redirect_url": "http://localhost/?x=1"},
                           headers=AUTH)
        return start.status, await start.json(), ok.status, await ok.json(), bad.status, await bad.text()

    start, start_body, ok, ok_body, bad, bad_text = _run(go)
    assert (start, start_body) == (200, {"auth_url": "https://auth.example.com/?state=x",
                                         "redirect_uri": "http://localhost"})
    assert (ok, ok_body) == (200, OAUTH_VIEW)
    assert bad == 400 and "secret detail" not in bad_text and "x=1" not in bad_text
    assert fake_oauth.finished == [("oa", "http://localhost/?code=abc")]


def test_host_login_unavailable_and_wrong_kind(fake_browser):
    async def go(c):
        ak = await c.post("/app/v1/sites/ak/host-login", headers=AUTH)
        fake_browser.is_available = False
        off = await c.post("/app/v1/sites/yt/host-login", headers=AUTH)
        return ak.status, off.status

    assert _run(go) == (400, 501)
    assert fake_browser.started == []


def test_providers_route_hides_config():
    async def go(c):
        anon = await c.get("/app/v1/sites/providers")
        r = await c.get("/app/v1/sites/providers", headers=AUTH)
        return anon.status, r.status, await r.json(), await r.text()

    anon, status, body, text = _run(go)
    assert (anon, status) == (401, 200)
    assert [p["id"] for p in body["providers"]] == ["google", "ebay"]
    ebay = body["providers"][1]
    assert set(ebay) == {"id", "name", "kind", "help", "flows", "fields"} and ebay["flows"] == ["safari"]
    assert [f["key"] for f in ebay["fields"]] == ["client_id", "client_secret", "redirect_param", "redirect_uri", "scopes"]
    assert "token_url" not in text and "auth_url" not in text and "token_auth" not in text


def test_host_login_refused_with_redirect_param(fake_sites, fake_oauth, fake_browser):
    fake_sites.views["eb"] = {**OAUTH_VIEW, "id": "eb", "redirect_param": "Ru-Name", "provider": "ebay"}

    async def go(c):
        r = await c.post("/app/v1/sites/eb/host-login", headers=AUTH)
        return r.status, await r.json(), set(routes._listeners)

    assert _run(go) == (400, {"error": "sign in with Safari for this site"}, set())
    assert fake_browser.started == [] and not hasattr(fake_oauth, "redirects")


def test_host_login_starts_browser(fake_oauth, fake_browser):
    async def go(c):
        cookie = await c.post("/app/v1/sites/yt/host-login", headers=AUTH)
        oauth = await c.post("/app/v1/sites/oa/host-login", headers=AUTH)
        listening = set(routes._listeners)
        routes._stop_all_listeners()
        return cookie.status, await cookie.json(), oauth.status, listening

    assert _run(go) == (200, {"ok": True}, 200, {"oa"})
    assert fake_browser.started == [("yt", VIEW["login_url"]), ("oa", "https://auth.example.com/?state=x")]
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", fake_oauth.redirects[0])


def test_host_login_finish_cookie(fake_sites, fake_browser):
    async def go(c):
        saved = await c.post("/app/v1/sites/yt/host-login/finish", json={"save": True}, headers=AUTH)
        text = await saved.text()
        dropped = await c.post("/app/v1/sites/yt/host-login/finish", json={"save": False}, headers=AUTH)
        return saved.status, text, dropped.status

    status, text, dropped = _run(go)
    assert (status, dropped) == (200, 200)
    assert json.loads(text) == VIEW and SECRET not in text
    assert fake_sites.saved == [("yt", [{"name": "SID", "value": SECRET}], "UA")]
    assert fake_sites.checked == ["yt"]
    assert fake_browser.closed == ["yt", "yt", "*"]


async def _hit(redirect: str, path: str) -> str:
    """One raw HTTP GET to the listener; the response text."""
    host, port = redirect.removeprefix("http://").rstrip("/").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    return raw.decode()


def test_host_login_oauth_listener_finishes_and_save_confirms(fake_oauth, fake_browser):
    async def go(c):
        await c.post("/app/v1/sites/oa/host-login", headers=AUTH)
        redirect = fake_oauth.redirects[0]
        early = await c.post("/app/v1/sites/oa/host-login/finish", json={"save": True}, headers=AUTH)
        # second round: the redirect arrives
        await c.post("/app/v1/sites/oa/host-login", headers=AUTH)
        redirect = fake_oauth.redirects[1]
        favicon = await _hit(redirect, "/favicon.ico")
        answer = await _hit(redirect, "/?code=abc&state=zz")
        ok = await c.post("/app/v1/sites/oa/host-login/finish", json={"save": True}, headers=AUTH)
        return redirect, early.status, favicon, answer, ok.status, await ok.json(), set(routes._listeners)

    redirect, early, favicon, answer, ok, body, left = _run(go)
    assert early == 400
    assert favicon.startswith("HTTP/1.1 404")
    assert "200 OK" in answer and "Signed in. You can close this window." in answer
    assert (ok, body, left) == (200, OAUTH_VIEW, set())
    assert fake_oauth.finished == [("oa", redirect + "?code=abc&state=zz")]
    assert fake_browser.closed == ["oa", "oa", "*"]


def test_host_login_oauth_failed_redirect_is_not_saved(fake_oauth, fake_browser):
    async def go(c):
        await c.post("/app/v1/sites/oa/host-login", headers=AUTH)
        answer = await _hit(fake_oauth.redirects[0], "/?error=access_denied")  # FakeOAuth.finish raises
        res = await c.post("/app/v1/sites/oa/host-login/finish", json={"save": True}, headers=AUTH)
        return answer, res.status

    answer, status = _run(go)
    assert "Sign-in failed" in answer and status == 400
    assert fake_oauth.finished == []


def test_cleanup_closes_host_browser(fake_browser):
    async def main():
        app = web.Application()
        routes.wire(app, FakeAdapter())
        async with TestClient(TestServer(app)):
            pass

    asyncio.run(main())
    assert fake_browser.closed == ["*"]
