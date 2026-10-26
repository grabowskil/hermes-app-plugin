import asyncio
import json
import os
import stat
import sys
import threading
import types
from http.cookiejar import MozillaCookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_app import site_providers, sites

SECRET = "s3cr3t-cookie-value"
UA = "Mozilla/5.0 (iPhone) Version/18.0 Mobile/15E148 Safari/604.1"


class Env:
    def __init__(self, server, state):
        self.server, self.state = server, state
        self.url = f"http://127.0.0.1:{server.server_port}/check"

    def add(self, site_id="yt", marker="LOGGED-IN"):
        return sites.upsert(site_id, "Site " + site_id, self.url, self.url, marker)

    def login(self, site_id="yt", value=SECRET):
        sites.save_session(site_id, [cookie("sid", value)], UA)

    def cookies_txt(self, site_id="yt"):
        return (sites._dir(site_id) / "cookies.txt").read_bytes()


def cookie(name="sid", value=SECRET, domain="127.0.0.1", expires=None, **kw):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires,
            "secure": False, "http_only": True, **kw}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sites, "_ALLOW_HTTP", True)
    state = {"status": 200, "body": b"hello LOGGED-IN", "set_cookie": None, "location": None,
             "seen": [], "auth": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["seen"].append({"cookie": self.headers.get("Cookie"), "ua": self.headers.get("User-Agent")})
            state["auth"].append(self.headers.get("Authorization"))
            self.send_response(state["status"])
            if state["location"]:
                self.send_header("Location", state["location"])
            if state["set_cookie"]:
                self.send_header("Set-Cookie", state["set_cookie"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        def server_bind(self):  # HTTPServer.server_bind does a slow getfqdn lookup
            self.socket.bind(self.server_address)
            self.server_port = self.socket.getsockname()[1]

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Env(server, state)
    server.shutdown()
    server.server_close()


def test_cookies_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sites, "_ALLOW_HTTP", True)
    sites.upsert("yt", "YouTube", "https://accounts.example.com/login", "https://www.example.com/x", "m")
    sites.save_session("yt", [
        cookie("dom", "a", ".example.com", expires=4102444800, secure=True),
        cookie("host", "b", "www.example.com"),
        cookie("sess", "c", "accounts.example.com", expires=None),
    ], UA)
    jar = MozillaCookieJar()
    jar.load(str(sites.stored_cookie_path("yt")), ignore_discard=True, ignore_expires=True)
    got = {c.name: c for c in jar}
    assert set(got) == {"dom", "host", "sess"}
    assert got["dom"].domain == ".example.com" and got["dom"].secure and got["dom"].expires == 4102444800
    assert got["host"].domain == "www.example.com" and not got["host"].domain_specified
    assert got["sess"].expires is None and got["sess"].discard
    assert got["host"].value == "b"


def test_ok_with_marker_sends_cookie_and_ua(env):
    env.add()
    env.login()
    v = env.cookies_txt()  # session exists before the check
    assert SECRET.encode() in v
    view = sites.check("yt")
    assert view["status"] == "ok" and view["detail"] is None and view["checked_at"]
    assert env.state["seen"][-1] == {"cookie": f"sid={SECRET}", "ua": UA}


def test_one_miss_stays_ok_two_expire_and_jar_not_saved(env):
    env.add()
    env.login()
    assert sites.check("yt")["status"] == "ok"
    before = env.cookies_txt()
    env.state.update(body=b"please sign in", set_cookie="rot=new; Path=/")
    assert sites.check("yt")["status"] == "ok"
    assert env.cookies_txt() == before
    view = sites.check("yt")
    assert view["status"] == "expired" and view["detail"] == "marker missing"
    assert env.cookies_txt() == before
    env.state.update(body=b"LOGGED-IN", set_cookie=None)
    assert sites.check("yt")["status"] == "ok"  # CHECK NOW recovers
    assert sites._read("yt")["misses"] == 0


def test_http_error_is_error_and_jar_not_saved(env):
    env.add()
    env.login()
    before = env.cookies_txt()
    env.state.update(status=503, set_cookie="rot=new; Path=/")
    view = sites.check("yt")
    assert view["status"] == "error" and view["detail"] == "HTTP 503"
    assert env.cookies_txt() == before
    assert sites._read("yt")["misses"] == 0


def test_unreachable_is_error(env):
    env.add()
    env.login()
    env.server.shutdown()
    env.server.server_close()
    assert sites.check("yt")["status"] == "error"


def test_rotated_cookie_saved_on_ok(env):
    env.add()
    env.login()
    env.state["set_cookie"] = "sid=rotated-value; Path=/"
    assert sites.check("yt")["status"] == "ok"
    assert b"rotated-value" in env.cookies_txt()
    assert SECRET.encode() not in env.cookies_txt()


def test_check_without_session_is_missing_and_no_request(env):
    env.add()
    assert sites.check("yt")["status"] == "missing"
    assert env.state["seen"] == []


def test_due_notices_once_per_expiry(env):
    env.add()
    env.login()
    env.state["body"] = b"nope"
    assert sites.due_notices() == []
    sites.check("yt")
    sites.check("yt")
    assert sites.get("yt")["status"] == "expired"
    first = sites.due_notices()
    assert len(first) == 1
    n = first[0]
    assert n["kind"] == "site" and n["title"] == "Login expired: Site yt" and n["job_id"] is None
    assert n["body"] == "Log in again: Settings → Sites → Site yt." and len(n["id"]) == 32
    assert sites.due_notices() == []
    env.login()
    sites.check("yt")
    sites.check("yt")
    assert len(sites.due_notices()) == 1


def test_foreign_domain_cookies_dropped(env):
    env.add()
    sites.save_session("yt", [cookie("a", "keep"), cookie("b", "evil", "evil.com"),
                              cookie("c", "evil2", "notlocalhost127.0.0.1")], UA)
    raw = env.cookies_txt().decode()
    assert "keep" in raw and "evil" not in raw
    with pytest.raises(ValueError):
        sites.save_session("yt", [cookie("b", "evil", "evil.com")], UA)


def test_bad_session_input(env):
    env.add()
    with pytest.raises(ValueError):
        sites.save_session("yt", [], UA)
    with pytest.raises(ValueError):
        sites.save_session("yt", [cookie(value="a\nb")], UA)
    with pytest.raises(ValueError):
        sites.save_session("yt", ["x"], UA)
    with pytest.raises(ValueError):
        sites.save_session("yt", [cookie()], "")
    with pytest.raises(KeyError):
        sites.save_session("nope", [cookie()], UA)


@pytest.mark.parametrize("bad", ["../x", "A", "", "-a", "a/b", "x" * 33, ".hidden"])
def test_bad_ids_rejected(env, bad):
    with pytest.raises(ValueError):
        sites.upsert(bad, "n", env.url, env.url, "m")
    assert sites.get(bad) is None
    for fn in (sites.check, sites.cookie_file, sites.stored_cookie_path):
        with pytest.raises(KeyError):
            fn(bad)
    with pytest.raises(KeyError):
        sites.save_session(bad, [cookie()], UA)


def test_views_have_no_cookie_values(env):
    env.add()
    env.login()
    sites.check("yt")
    for view in (sites.get("yt"), sites.list_sites()[0], sites.check("yt")):
        assert SECRET not in json.dumps(view)
        assert set(view) == {"id", "name", "kind", "login_url", "check_domain", "has_secret", "status",
                             "checked_at", "saved_at", "detail", "check_url", "marker", "provider", "flows"}
        assert view["kind"] == "cookie" and view["has_secret"] is True
    assert sites.get("yt")["check_domain"] == "127.0.0.1"


def test_upsert_validation_and_host_change(env, monkeypatch):
    env.add()
    env.login()
    # same host: session kept, fields updated
    v = sites.upsert("yt", "Renamed", env.url, env.url + "?x=1", "M2")
    assert v["name"] == "Renamed" and v["saved_at"]
    assert (sites._dir("yt") / "cookies.txt").exists()
    # different host: session dropped
    v = sites.upsert("yt", "Renamed", env.url, "http://localhost:1/c", "M2")
    assert v["status"] == "missing" and v["saved_at"] is None
    assert not (sites._dir("yt") / "cookies.txt").exists()
    monkeypatch.setattr(sites, "_ALLOW_HTTP", False)
    with pytest.raises(ValueError) as e:
        sites.upsert("web", "W", "http://example.com/login", "https://example.com/c", "m")
    assert "example.com" not in str(e.value)
    with pytest.raises(ValueError):
        sites.upsert("web", "W", "https://example.com/login", "https://example.com/c", "")
    with pytest.raises(ValueError):
        sites.upsert("web", " ", "https://example.com/login", "https://example.com/c", "m")
    assert sites.get("web") is None


def test_list_sorted_by_name(env):
    sites.upsert("b", "Beta", env.url, env.url, "m")
    sites.upsert("a", "alpha", env.url, env.url, "m")
    sites.upsert("c", "Charlie", env.url, env.url, "m")
    assert [s["id"] for s in sites.list_sites()] == ["a", "b", "c"]


def test_cookie_file_is_fresh_private_copy(env):
    env.add()
    env.login()
    a, b = sites.cookie_file("yt"), sites.cookie_file("yt")
    try:
        assert a != b and a != sites.stored_cookie_path("yt")
        assert a.read_bytes() == env.cookies_txt()
        assert stat.S_IMODE(a.stat().st_mode) == 0o600
        a.write_text("clobbered")
        assert SECRET.encode() in env.cookies_txt()
    finally:
        a.unlink()
        b.unlink()


def test_file_modes(env):
    env.add()
    env.login()
    d = sites._dir("yt")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for name in ("site.json", "cookies.txt", ".lock"):
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


def test_check_discards_result_when_new_session_arrives(env, monkeypatch):
    env.add()
    env.login()
    real = sites._fetch

    def fetch_then_relogin(*a, **kw):
        body = real(*a, **kw)
        sites.save_session("yt", [cookie("sid", "fresh")], UA)
        return body

    monkeypatch.setattr(sites, "_fetch", fetch_then_relogin)
    view = sites.check("yt")
    assert view["status"] == "error" and view["detail"] == "not checked"
    assert b"fresh" in env.cookies_txt()


def test_keepalive_checks_due_sites_and_publishes_once(env, monkeypatch):
    env.add()
    env.login()
    env.state["body"] = b"nope"
    sites.check("yt")
    sites.check("yt")  # expired, not yet announced
    env.add("fresh")
    env.login("fresh")
    monkeypatch.setattr(sites, "POLL_S", 0.01)
    published = []

    async def run():
        task = asyncio.create_task(sites.keepalive(published.append))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert [n["title"] for n in published] == ["Login expired: Site yt"]
    assert sites.get("fresh")["checked_at"]  # was due and got checked
    assert len(env.state["seen"]) == 2 + 1


def test_keepalive_survives_errors(env, monkeypatch):
    monkeypatch.setattr(sites, "POLL_S", 0.01)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("x")

    monkeypatch.setattr(sites, "_due_ids", boom)

    async def run():
        task = asyncio.create_task(sites.keepalive(lambda n: None))
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()

    asyncio.run(run())
    assert len(calls) > 1


def test_cli(env, capsys):
    import argparse

    parser = argparse.ArgumentParser()
    sites.setup_cli(parser.add_subparsers())
    args = parser.parse_args(["sites", "add", "yt", "--name", "YouTube", "--login-url", env.url,
                              "--check-url", env.url, "--marker", "m"])
    assert args.func(args) == 0
    args = parser.parse_args(["sites", "list"])
    assert args.func(args) == 0
    assert "yt\tmissing\tYouTube\t127.0.0.1\tcookie" in capsys.readouterr().out
    args = parser.parse_args(["sites", "path", "yt"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip().endswith("sites/yt/cookies.txt")
    args = parser.parse_args(["sites", "path", "nope"])
    assert args.func(args) == 1


# --- plan 0008: api key and oauth kinds -------------------------------------------------------

KEY = "k3y-secret-value"
CLIENT_SECRET = "cl13nt-s3cr3t"


@pytest.fixture
def oauth_stub(monkeypatch):
    mod = types.ModuleType("hermes_app.site_oauth")
    mod.GOOGLE = dict(site_providers.PROVIDERS["google"]["config"])  # providers own the config now
    mod.calls = []

    def refresh(site_id):
        mod.calls.append(site_id)
        with sites._locked(site_id):
            meta = sites._read(site_id)
            meta.update(status="ok", detail=None, checked_at=sites._now())
            sites._store(site_id, meta)
        return "ok"

    mod.refresh = refresh
    mod.access_token = lambda site_id: "access-" + site_id
    monkeypatch.setitem(sys.modules, "hermes_app.site_oauth", mod)
    return mod


def add_key_site(env, site_id="api", with_check=True, **kw):
    fields = {"id": site_id, "name": "Api " + site_id, "kind": "api_key", **kw}
    if with_check:
        fields.setdefault("check_url", env.url)
    return sites.put(fields)


def test_api_key_ok_sends_header_and_stores_key_privately(env):
    v = add_key_site(env)
    assert v["status"] == "missing" and v["has_secret"] is False
    v = sites.set_api_key("api", f"  {KEY}\n")
    assert v["has_secret"] is True and KEY not in json.dumps(v)
    assert stat.S_IMODE((sites._dir("api") / "secret.json").stat().st_mode) == 0o600
    assert sites.secret("api") == KEY
    v = sites.check("api")
    assert v["status"] == "ok" and v["detail"] is None and v["checked_at"]
    assert env.state["auth"][-1] == f"Bearer {KEY}"
    assert json.loads((sites._dir("api") / "secret.json").read_text()) == {"api_key": KEY}


def test_api_key_written_before_2_2_1_is_still_read(env):
    add_key_site(env)
    (sites._dir("api") / "secret.json").write_text(json.dumps({"key": KEY}))
    assert sites.secret("api") == KEY and sites.get("api")["has_secret"] is True


def test_api_key_custom_header_and_prefix(env):
    add_key_site(env, header="X-Api-Key", prefix="")
    sites.set_api_key("api", KEY)
    assert sites.check("api")["status"] == "ok"
    assert env.state["auth"][-1] is None  # not sent as Authorization


@pytest.mark.parametrize("code", [401, 403])
def test_api_key_rejected_is_expired_at_once(env, code):
    add_key_site(env)
    sites.set_api_key("api", KEY)
    env.state["status"] = code
    v = sites.check("api")
    assert v["status"] == "expired" and v["detail"] == f"HTTP {code}"
    assert [n["title"] for n in sites.due_notices()] == ["Login expired: Api api"]
    env.state["status"] = 200
    assert sites.check("api")["status"] == "ok"


def test_api_key_server_error_is_error(env):
    add_key_site(env)
    sites.set_api_key("api", KEY)
    env.state["status"] = 503
    v = sites.check("api")
    assert v["status"] == "error" and v["detail"] == "HTTP 503"


def test_api_key_redirect_is_not_followed(env):
    add_key_site(env)
    sites.set_api_key("api", KEY)
    env.state.update(status=302, location=env.url.replace("/check", "/elsewhere"))
    v = sites.check("api")
    assert v["status"] == "error" and v["detail"] == "redirect not followed"
    assert len(env.state["seen"]) == 1 and len(env.state["auth"]) == 1  # the key was sent exactly once


def test_api_key_without_check_url_is_ok_not_checked(env):
    add_key_site(env, with_check=False)
    v = sites.set_api_key("api", KEY)
    assert v["status"] == "ok" and v["detail"] == "not checked" and v["check_domain"] is None
    v = sites.check("api")
    assert v["status"] == "ok" and v["detail"] == "not checked"
    assert env.state["seen"] == []


def test_api_key_check_without_key_makes_no_request(env):
    add_key_site(env)
    assert sites.check("api")["status"] == "missing"
    assert env.state["seen"] == []


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "k\u00e9y", "x" * 5000, None])
def test_set_api_key_validation(env, bad):
    add_key_site(env)
    with pytest.raises(ValueError):
        sites.set_api_key("api", bad)
    with pytest.raises(KeyError):
        sites.set_api_key("nope", KEY)
    env.add("cook")
    with pytest.raises(ValueError):
        sites.set_api_key("cook", KEY)
    assert sites.get("api")["has_secret"] is False


def test_put_slug_defaults_and_update(env):
    v = sites.put({"name": "My Cool API!", "kind": "api-key"})
    assert v["id"] == "my-cool-api" and v["kind"] == "api_key"
    assert v["header"] == "Authorization" and v["prefix"] == "Bearer " and v["check_url"] is None
    with pytest.raises(ValueError):
        sites.put({"name": "!!!"})
    v = sites.put({"id": "my-cool-api", "name": "Renamed", "check_url": env.url})
    assert v["name"] == "Renamed" and v["check_url"] == env.url and v["header"] == "Authorization"
    assert len(sites.list_sites()) == 1


@pytest.mark.parametrize("fields", [
    {"name": "A", "kind": "bogus"},
    {"name": "A", "kind": "api_key", "header": "Bad Header"},
    {"name": "A", "kind": "api_key", "check_url": "http://insecure.example.com/x"},
    {"name": "A", "kind": "api_key", "prefix": "a\nb"},
    {"name": "A", "kind": "api_key", "client_secret": "x"},
    {"name": "A", "kind": "cookie", "login_url": "https://e.com/l", "check_url": "https://e.com/c"},  # no marker
    {"name": "A", "kind": "oauth", "auth_url": "https://e.com/a", "token_url": "https://e.com/t", "client_id": "c",
     "redirect_uri": "http://evil.example.com/cb"},
    {"name": "A", "kind": "oauth", "auth_url": "http://e.com/a", "token_url": "https://e.com/t", "client_id": "c",
     "redirect_uri": "http://localhost"},
    {"name": "A", "kind": "oauth", "auth_url": "https://e.com/a", "token_url": "https://e.com/t", "client_id": "",
     "redirect_uri": "http://localhost"},
    {"name": "A", "kind": "oauth", "preset": "nope"},
    {"name": "A", "kind": "api_key", "preset": "google"},
    {"name": "x" * 65},
    "not a dict",
])
def test_put_rejects_bad_fields_and_leaves_nothing(env, monkeypatch, fields):
    monkeypatch.setattr(sites, "_ALLOW_HTTP", False)
    with pytest.raises(ValueError):
        sites.put(fields)
    assert sites.list_sites() == []
    assert not (sites._data_dir() / "sites" / "a").exists()


def test_put_host_change_drops_credentials(env):
    add_key_site(env, check_url="https://api.example.com/v1/me")
    sites.set_api_key("api", KEY)
    # same host: key kept, fields updated
    v = sites.put({"id": "api", "name": "Api", "check_url": "https://api.example.com/v2/me"})
    assert v["has_secret"] is True and v["check_url"].endswith("/v2/me")
    # new host: key dropped
    v = sites.put({"id": "api", "name": "Api", "check_url": "https://other.example.org/me"})
    assert v["has_secret"] is False and v["status"] == "missing"
    assert not (sites._dir("api") / "secret.json").exists()


def test_put_first_check_url_keeps_key_and_kind_change_drops_it(env):
    add_key_site(env, with_check=False)
    sites.set_api_key("api", KEY)
    v = sites.put({"id": "api", "name": "Api", "check_url": env.url})
    assert v["has_secret"] is True
    v = sites.put({"id": "api", "name": "Api", "kind": "cookie", "login_url": env.url, "check_url": env.url,
                   "marker": "m"})
    assert v["kind"] == "cookie" and v["has_secret"] is False
    assert not (sites._dir("api") / "secret.json").exists()


def test_cookie_host_change_drops_cookies_and_pending(env):
    env.add()
    env.login()
    (sites._dir("yt") / "pending.json").write_text("{}")
    sites.put({"id": "yt", "name": "Site yt", "login_url": env.url, "check_url": "http://localhost:1/c",
               "marker": "LOGGED-IN"})
    d = sites._dir("yt")
    assert not (d / "cookies.txt").exists() and not (d / "pending.json").exists()


def test_oauth_put_google_preset_and_client_secret(env, oauth_stub):
    v = sites.put({"id": "gyt", "name": "YouTube API", "preset": "google", "client_id": "cid",
                   "client_secret": CLIENT_SECRET, "scopes": "a b", "extra_params": "evil=1"})
    assert v["kind"] == "oauth" and v["auth_url"] == oauth_stub.GOOGLE["auth_url"]
    assert v["token_url"] == oauth_stub.GOOGLE["token_url"] and v["redirect_uri"] == "http://localhost"
    assert v["client_id"] == "cid" and v["scopes"] == "a b"
    assert v["has_client_secret"] is True and v["has_secret"] is False
    assert sites._read("gyt")["extra_params"] == "access_type=offline&prompt=consent"
    assert CLIENT_SECRET not in json.dumps(sites._read("gyt"))  # site.json never holds it
    assert sites.read_secret("gyt") == {"client_secret": CLIENT_SECRET}
    # updating without a client_secret keeps it; a new token host drops everything
    sites.put({"id": "gyt", "name": "YouTube API"})
    assert sites.get("gyt")["has_client_secret"] is True
    v = sites.put({"id": "gyt", "name": "YouTube API", "token_url": "https://evil.example.net/token"})
    assert v["has_client_secret"] is False


def test_secret_helpers_merge_and_are_private(env):
    env.add()
    sites.write_secret("yt", {"a": 1, "b": 2})
    sites.write_secret("yt", {"b": 3, "c": None})
    assert sites.read_secret("yt") == {"a": 1, "b": 3}
    assert stat.S_IMODE((sites._dir("yt") / "secret.json").stat().st_mode) == 0o600
    assert sites.read_secret("nope") == {} and sites.read_secret("../x") == {}
    with pytest.raises(KeyError):
        sites.write_secret("nope", {"a": 1})
    with pytest.raises(ValueError):
        sites.secret("yt")  # cookie sites have none


def test_oauth_check_secret_and_view(env, oauth_stub):
    sites.put({"id": "g", "name": "G", "preset": "google", "client_id": "cid"})
    v = sites.check("g")
    assert v["status"] == "missing" and oauth_stub.calls == []  # no refresh token yet
    sites.write_secret("g", {"refresh_token": "r3fresh-secret", "access_token": "acc-secret"})
    v = sites.check("g")
    assert oauth_stub.calls == ["g"] and v["status"] == "ok" and v["has_secret"] is True
    assert sites.secret("g") == "access-g"
    for text in (json.dumps(v), json.dumps(sites.list_sites())):
        assert "r3fresh-secret" not in text and "acc-secret" not in text
    assert set(v) == {"id", "name", "kind", "login_url", "check_domain", "has_secret", "status", "checked_at",
                      "saved_at", "detail", "auth_url", "token_url", "client_id", "scopes", "redirect_uri",
                      "has_client_secret", "provider", "flows", "redirect_param"}


def test_views_of_every_kind_have_no_secret_values(env, oauth_stub):
    add_key_site(env)
    sites.set_api_key("api", KEY)
    sites.put({"id": "g", "name": "G", "preset": "google", "client_id": "cid", "client_secret": CLIENT_SECRET})
    sites.write_secret("g", {"refresh_token": "rt-secret"})
    env.add()
    env.login()
    dump = json.dumps([sites.list_sites(), sites.check("api"), sites.get("g"), sites.get("yt")])
    for value in (KEY, CLIENT_SECRET, "rt-secret", SECRET):
        assert value not in dump
    assert {v["id"]: v["kind"] for v in sites.list_sites()} == {"api": "api_key", "g": "oauth", "yt": "cookie"}


def test_view_tolerates_missing_fields(env):
    d = sites._dir("legacy")
    sites._mkdir(d)
    sites._store("legacy", {"id": "legacy", "name": "Legacy", "kind": "api_key"})
    v = sites.get("legacy")
    assert v["login_url"] is None and v["check_domain"] is None and v["status"] == "missing"
    sites._store("legacy", {"id": "legacy", "name": "Legacy"})  # 0007 site without any urls
    assert sites.get("legacy")["kind"] == "cookie"
    assert [s["id"] for s in sites.list_sites()] == ["legacy"]


def test_remove(env):
    env.add()
    env.login()
    assert sites.remove("yt") is True
    assert sites.get("yt") is None and not sites._dir("yt").exists()
    assert sites.remove("yt") is False and sites.remove("../x") is False


def test_due_ids_per_kind(env, oauth_stub):
    env.add("cook")
    add_key_site(env, "keyed")
    add_key_site(env, "nokey")
    sites.put({"id": "goa", "name": "Goa", "preset": "google", "client_id": "c"})
    sites.put({"id": "goa2", "name": "Goa2", "preset": "google", "client_id": "c"})
    assert sites._due_ids() == []  # nothing has a credential
    env.login("cook")
    sites.set_api_key("keyed", KEY)
    sites.write_secret("goa", {"refresh_token": "rt"})
    sites.write_secret("goa2", {"access_token": "only-access"})  # no refresh token: not due
    # a status other than ok/error (here: expired) is not checked either
    with sites._locked("goa2"):
        meta = sites._read("goa2")
        meta["status"] = "expired"
        sites._store("goa2", meta)
    with sites._locked("goa"):
        meta = sites._read("goa")
        meta["status"] = "ok"
        sites._store("goa", meta)
    assert sorted(sites._due_ids()) == ["cook", "goa", "keyed"]
    sites.check("keyed")
    sites.check("cook")
    assert sorted(sites._due_ids()) == ["goa"]  # recently checked ones drop out


def test_cli_add_oauth_with_client_json_and_secret(env, oauth_stub, tmp_path, capsys):
    import argparse

    parser = argparse.ArgumentParser()
    sites.setup_cli(parser.add_subparsers())
    cj = tmp_path / "client.json"
    cj.write_text(json.dumps({"installed": {"client_id": "cid.apps", "client_secret": CLIENT_SECRET,
                                            "redirect_uris": ["http://localhost:8080"]}}))
    args = parser.parse_args(["sites", "add", "gyt", "--name", "YouTube API", "--preset", "google",
                              "--scopes", "https://www.googleapis.com/auth/youtube.readonly",
                              "--client-json", str(cj)])
    assert args.func(args) == 0
    out = capsys.readouterr()
    assert CLIENT_SECRET not in out.out + out.err
    v = sites.get("gyt")
    assert v["kind"] == "oauth" and v["client_id"] == "cid.apps" and v["has_client_secret"] is True
    assert v["redirect_uri"] == "http://localhost:8080"  # the client file wins over the preset
    assert v["auth_url"] == oauth_stub.GOOGLE["auth_url"]
    assert sites.read_secret("gyt")["client_secret"] == CLIENT_SECRET
    # "web" clients work too; a broken file fails without echoing its content
    cj.write_text(json.dumps({"web": {"client_id": "web.apps", "client_secret": "s"}}))
    args = parser.parse_args(["sites", "add", "gweb", "--name", "W", "--preset", "google", "--client-json", str(cj)])
    assert args.func(args) == 0 and sites.get("gweb")["client_id"] == "web.apps"
    cj.write_text(json.dumps({"other": {"client_secret": CLIENT_SECRET}}))
    args = parser.parse_args(["sites", "add", "bad", "--name", "B", "--preset", "google", "--client-json", str(cj)])
    assert args.func(args) == 1
    assert CLIENT_SECRET not in capsys.readouterr().err and sites.get("bad") is None


def test_cli_api_key_flow(env, tmp_path, monkeypatch, capsys):
    import argparse
    import io

    parser = argparse.ArgumentParser()
    sites.setup_cli(parser.add_subparsers())

    def run(*argv):
        args = parser.parse_args(["sites", *argv])
        return args.func(args)

    assert run("add", "api", "--name", "Api", "--kind", "api-key", "--check-url", env.url) == 0
    kf = tmp_path / "key.txt"
    kf.write_text(KEY + "\n")
    assert run("set-key", "api", "--from-file", str(kf)) == 0
    assert run("set-key", "api", "--from-file", str(tmp_path / "missing")) == 1
    monkeypatch.setattr(sys, "stdin", io.StringIO("from-stdin-key\n"))
    assert run("set-key", "api") == 0
    assert sites.secret("api") == "from-stdin-key"
    assert run("set-key", "nope", "--from-file", str(kf)) == 1
    capsys.readouterr()
    assert run("secret", "api") == 0
    assert capsys.readouterr().out.strip() == "from-stdin-key"
    assert run("secret", "nope") == 1
    env.add()
    assert run("secret", "yt") == 1
    assert run("remove", "api") == 0 and run("remove", "api") == 1
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("secret", "-h")
    assert "SENSITIVE" in capsys.readouterr().out


# --- plan 0009: providers ---------------------------------------------------------------------

EBAY = {"name": "eBay", "provider": "ebay", "client_id": "app-id", "client_secret": CLIENT_SECRET,
        "redirect_param": "Ru-Name-1"}


def test_provider_put_ebay(env):
    v = sites.put({"id": "ebay", **EBAY, "extra_params": "evil=1", "auth_url": "https://evil.example/a",
                   "token_auth": "body", "pkce": False, "bogus": "x"})
    meta = sites._read("ebay")
    cfg = site_providers.PROVIDERS["ebay"]["config"]
    assert meta["provider"] == "ebay" and meta["kind"] == "oauth"
    assert (meta["auth_url"], meta["token_url"]) == (cfg["auth_url"], cfg["token_url"])  # request urls dropped
    assert (meta["token_auth"], meta["pkce"], meta["refresh_scope"]) == ("basic", True, True)
    assert meta["extra_params"] is None and "bogus" not in meta
    assert meta["redirect_param"] == "Ru-Name-1" and meta["redirect_uri"] == "https://localhost/ebay"
    assert meta["scopes"].startswith("https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.")
    assert CLIENT_SECRET not in json.dumps(meta) and sites.read_secret("ebay") == {"client_secret": CLIENT_SECRET}
    assert (v["provider"], v["flows"], v["redirect_param"]) == ("ebay", ["safari"], "Ru-Name-1")
    assert "token_auth" not in v and CLIENT_SECRET not in json.dumps(v)
    # update keeps the credentials and the config
    sites.put({"id": "ebay", "name": "eBay", "provider": "ebay", "client_id": "app-id", "redirect_param": "Ru-Name-1"})
    assert sites.read_secret("ebay") == {"client_secret": CLIENT_SECRET}


@pytest.mark.parametrize("extra", [{"redirect_param": ""}, {"redirect_param": "a\nb"}, {"redirect_param": "x" * 257},
                                   {"client_id": ""}, {"provider": "nope"}])
def test_provider_put_rejects_bad_input(env, extra):
    with pytest.raises(ValueError):
        sites.put({"id": "ebay", **{**EBAY, **extra}})
    assert sites.get("ebay") is None


def test_redirect_param_only_via_provider_or_cli(env, oauth_stub):
    custom = {"id": "c", "name": "C", "kind": "oauth", "auth_url": "https://e.com/a", "token_url": "https://e.com/t",
              "client_id": "c", "redirect_uri": "http://localhost"}
    sites.put({**custom, "redirect_param": "RuName", "token_auth": "basic", "pkce": False})
    meta = sites._read("c")
    assert not {"redirect_param", "token_auth", "pkce", "refresh_scope", "provider"} & set(meta)
    # Google lists no redirect_param: dropped
    sites.put({"id": "g", "name": "G", "provider": "google", "client_id": "cid", "redirect_param": "x"})
    assert "redirect_param" not in sites._read("g")
    # the CLI path may set it
    assert sites.put({**custom, "redirect_param": "RuName"}, local=True)["redirect_param"] == "RuName"
    # and an update through the app keeps it
    sites.put({**custom, "name": "C2"})
    assert sites._read("c")["redirect_param"] == "RuName"


def test_preset_alias_equals_provider_google_and_is_byte_compatible(env, oauth_stub):
    body = {"name": "YT", "client_id": "cid", "client_secret": CLIENT_SECRET, "scopes": "a b"}
    sites.put({"id": "pv", **body, "provider": "google"})
    sites.put({"id": "ps", **body, "preset": "google"})
    pv, ps = sites._read("pv"), sites._read("ps")
    assert pv["provider"] == ps["provider"] == "google"
    strip = lambda m: {k: v for k, v in m.items() if k not in ("id", "name", "saved_at", "checked_at")}
    assert strip(pv) == strip(ps)
    # what 2.1.0 wrote for a Google preset, key for key (plus "provider" and nothing else)
    assert list(pv) == ["status", "checked_at", "saved_at", "detail", "misses", "notified", "user_agent",
                        "id", "name", "kind", "extra_params", "auth_url", "token_url", "client_id", "scopes",
                        "redirect_uri", "provider"]
    assert pv["extra_params"] == "access_type=offline&prompt=consent" and pv["redirect_uri"] == "http://localhost"
    assert pv["auth_url"] == "https://accounts.google.com/o/oauth2/v2/auth"
    # switching an existing site to another provider keeps nothing of the first
    sites.put({"id": "pv", **EBAY})
    assert sites._read("pv")["provider"] == "ebay" and sites._read("pv")["scopes"].startswith("https://api.ebay.com/")


def test_views_carry_provider_and_flows(env):
    env.add()
    v = sites.get("yt")
    assert v["provider"] is None and v["flows"] is None
    sites.put({"id": "ebay", **EBAY})
    assert [x["flows"] for x in sites.list_sites() if x["id"] == "ebay"] == [["safari"]]


def test_public_list_hides_config():
    out = site_providers.public_list()
    assert [p["id"] for p in out] == ["google", "ebay"]
    for p in out:
        assert set(p) == {"id", "name", "kind", "help", "flows", "fields"}
        assert all(f["key"] in site_providers.FIELD_KEYS for f in p["fields"])
    assert "token_url" not in json.dumps(out)


def test_cli_provider_flag_alias_redirect_param_and_list(env, capsys):
    import argparse

    parser = argparse.ArgumentParser()
    sites.setup_cli(parser.add_subparsers())
    args = parser.parse_args(["sites", "add", "ebay", "--name", "eBay", "--provider", "ebay", "--client-id", "app",
                              "--redirect-param", "Ru-Name-9"])
    assert args.func(args) == 0
    v = sites.get("ebay")
    assert (v["provider"], v["redirect_param"], v["client_id"]) == ("ebay", "Ru-Name-9", "app")
    args = parser.parse_args(["sites", "add", "g", "--name", "G", "--preset", "google", "--client-id", "c"])
    assert args.func(args) == 0 and sites.get("g")["provider"] == "google"
    capsys.readouterr()
    args = parser.parse_args(["sites", "providers"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.splitlines() == ["google\tGoogle", "ebay\teBay"]
