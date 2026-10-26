import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from hermes_app import site_oauth, sites


def _fallback_read_secret(site_id):
    try:
        return json.loads((sites._dir(site_id) / "secret.json").read_text())
    except (OSError, ValueError):
        return {}


def _fallback_write_secret(site_id, data):
    with sites._locked(site_id):
        merged = {**_fallback_read_secret(site_id), **data}
        sites._write(sites._dir(site_id) / "secret.json", json.dumps(merged).encode())


class Env:
    def __init__(self, server, state):
        self.server, self.state = server, state
        self.token_url = f"http://127.0.0.1:{server.server_port}/token"

    def add(self, site_id="gg", **extra):
        meta = {"id": site_id, "name": "G", "kind": "oauth", "auth_url": "https://auth.example/authorize",
                "token_url": self.token_url, "client_id": "cid", "scopes": "a b",
                "redirect_uri": "http://localhost", "extra_params": "access_type=offline&prompt=consent",
                "status": "missing", **extra}
        with sites._locked(site_id):
            sites._store(site_id, meta)

    def redirect(self, site_id="gg", state=None, code="the-code"):
        pending = json.loads((sites._dir(site_id) / "pending.json").read_text())
        return f"http://localhost/?code={code}&state={state or pending['state']}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path))
    if not hasattr(sites, "read_secret"):
        monkeypatch.setattr(sites, "read_secret", _fallback_read_secret, raising=False)
        monkeypatch.setattr(sites, "write_secret", _fallback_write_secret, raising=False)
    state = {"status": 200, "body": {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600},
             "forms": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            state["forms"].append({k: v[0] for k, v in parse_qs(raw.decode()).items()})
            payload = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Env(server, state)
    server.shutdown()
    server.server_close()


def test_start_has_pkce_and_params(env):
    env.add()
    out = site_oauth.start("gg")
    assert out["redirect_uri"] == "http://localhost"
    q = {k: v[0] for k, v in parse_qs(urlsplit(out["auth_url"]).query).items()}
    assert q["response_type"] == "code" and q["client_id"] == "cid" and q["scope"] == "a b"
    assert q["redirect_uri"] == "http://localhost" and q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    pending = json.loads((sites._dir("gg") / "pending.json").read_text())
    assert pending["state"] == q["state"]
    digest = hashlib.sha256(pending["verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]
    assert (sites._dir("gg") / "pending.json").stat().st_mode & 0o777 == 0o600


def test_finish_exchanges_and_stores(env):
    env.add()
    site_oauth.start("gg")
    sites.write_secret("gg", {"client_secret": "csec"})
    pending = json.loads((sites._dir("gg") / "pending.json").read_text())
    site_oauth.finish("gg", env.redirect())
    form = env.state["forms"][0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["code_verifier"] == pending["verifier"] and form["redirect_uri"] == "http://localhost"
    assert form["client_id"] == "cid" and form["client_secret"] == "csec"
    secret = sites.read_secret("gg")
    assert secret["refresh_token"] == "RT1" and secret["access_token"] == "AT1" and secret["expires_at"]
    assert secret["client_secret"] == "csec"
    assert not (sites._dir("gg") / "pending.json").exists()
    assert sites._read("gg")["status"] == "ok"


def test_state_mismatch(env):
    env.add()
    site_oauth.start("gg")
    with pytest.raises(ValueError) as e:
        site_oauth.finish("gg", env.redirect(state="wrong", code="SECRETCODE"))
    assert "SECRETCODE" not in str(e.value)
    assert env.state["forms"] == []


def test_finish_rejects_expired_pending_wrong_page_and_error(env):
    env.add()
    site_oauth.start("gg")
    pending_path = sites._dir("gg") / "pending.json"
    pending = json.loads(pending_path.read_text())
    url = env.redirect()
    with pytest.raises(ValueError):
        site_oauth.finish("gg", "http://evil.example/?code=x&state=" + pending["state"])
    with pytest.raises(ValueError, match="refused: access_denied"):
        site_oauth.finish("gg", "http://localhost/?error=access_denied")
    pending["expires_at"] = time.time() - 1
    pending_path.write_text(json.dumps(pending))
    with pytest.raises(ValueError, match="expired"):
        site_oauth.finish("gg", url)
    assert env.state["forms"] == []


def test_finish_token_error(env):
    env.add()
    site_oauth.start("gg")
    env.state.update(status=400, body={"error": "invalid_grant", "error_description": "TOKENBODY"})
    with pytest.raises(ValueError) as e:
        site_oauth.finish("gg", env.redirect())
    assert "TOKENBODY" not in str(e.value) and "invalid_grant" in str(e.value)


def test_refresh_rotates_refresh_token(env):
    env.add()
    sites.write_secret("gg", {"refresh_token": "RT-old", "client_secret": "csec"})
    env.state["body"] = {"access_token": "AT2", "refresh_token": "RT-new", "expires_in": 100}
    assert site_oauth.refresh("gg") == "ok"
    form = env.state["forms"][0]
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "RT-old"
    secret = sites.read_secret("gg")
    assert secret["refresh_token"] == "RT-new" and secret["access_token"] == "AT2"
    assert secret["client_secret"] == "csec"
    meta = sites._read("gg")
    assert meta["status"] == "ok" and meta["checked_at"]


def test_refresh_keeps_refresh_token_when_not_rotated(env):
    env.add()
    sites.write_secret("gg", {"refresh_token": "RT-old"})
    env.state["body"] = {"access_token": "AT2", "expires_in": 100}
    assert site_oauth.refresh("gg") == "ok"
    assert sites.read_secret("gg")["refresh_token"] == "RT-old"


def test_refresh_invalid_grant_is_expired(env):
    env.add()
    sites.write_secret("gg", {"refresh_token": "RT"})
    env.state.update(status=400, body={"error": "invalid_grant"})
    assert site_oauth.refresh("gg") == "expired"
    assert sites._read("gg")["status"] == "expired"


def test_refresh_server_error_and_network(env):
    env.add()
    sites.write_secret("gg", {"refresh_token": "RT"})
    env.state.update(status=503, body={})
    assert site_oauth.refresh("gg") == "error"
    assert sites._read("gg")["status"] == "error"
    env.server.shutdown()
    env.server.server_close()
    assert site_oauth.refresh("gg") == "error"
    assert sites._read("gg")["detail"] == "network error"


def test_access_token_refreshes_only_when_close_to_expiry(env):
    env.add()
    soon = site_oauth._iso(time.time() + 60)
    later = site_oauth._iso(time.time() + 3600)
    sites.write_secret("gg", {"refresh_token": "RT", "access_token": "old", "expires_at": later})
    assert site_oauth.access_token("gg") == "old" and env.state["forms"] == []
    sites.write_secret("gg", {"expires_at": soon})
    assert site_oauth.access_token("gg") == "AT1" and len(env.state["forms"]) == 1


@pytest.mark.parametrize("url,expected", [
    ("http://localhost/?code=x", True),
    ("http://localhost:80/path?code=x", True),
    ("HTTP://LOCALHOST/", True),
    ("http://localhost:8080/?code=x", False),
    ("https://localhost/?code=x", False),
    ("http://127.0.0.1/?code=x", False),
    ("http://localhost.evil.com/", False),
    ("not a url", False),
    ("", False),
])
def test_matches_redirect(url, expected):
    assert site_oauth.matches_redirect(url, "http://localhost") is expected


def test_matches_redirect_ports():
    assert site_oauth.matches_redirect("https://a.example:443/x", "https://a.example")
    assert site_oauth.matches_redirect("http://localhost:8080/x", "http://localhost:8080")
    assert not site_oauth.matches_redirect("http://localhost/x", "http://localhost:8080")
    assert not site_oauth.matches_redirect("http://localhost:99999/x", "http://localhost")


def test_google_preset():
    assert site_oauth.GOOGLE["redirect_uri"] == "http://localhost"
    assert site_oauth.GOOGLE["extra_params"] == "access_type=offline&prompt=consent"
