import asyncio
import json
import os
import stat
import threading
from http.cookiejar import MozillaCookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_app import sites

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
    state = {"status": 200, "body": b"hello LOGGED-IN", "set_cookie": None, "seen": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["seen"].append({"cookie": self.headers.get("Cookie"), "ua": self.headers.get("User-Agent")})
            self.send_response(state["status"])
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
        assert set(view) == {"id", "name", "login_url", "check_domain", "status", "checked_at",
                             "saved_at", "detail"}
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
    assert "yt\tmissing\tYouTube" in capsys.readouterr().out
    args = parser.parse_args(["sites", "path", "yt"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip().endswith("sites/yt/cookies.txt")
    args = parser.parse_args(["sites", "path", "nope"])
    assert args.func(args) == 1
