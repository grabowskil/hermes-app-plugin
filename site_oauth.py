"""OAuth 2.0 for sites (authorization code + PKCE S256), tokens kept for skills.

``start`` makes the authorization URL and keeps ``state`` and the PKCE verifier in ``pending.json``
(10 minutes). The user signs in elsewhere (host browser or Safari) and ends on a redirect page that
fails to load; its address carries ``code`` and ``state`` and goes to ``finish``. Tokens live in the
site's ``secret.json`` (``refresh_token``, ``access_token``, ``expires_at``, optional
``client_secret``). ``refresh`` does its HTTP call outside the site lock and merges under it.
Neither URLs with codes nor token responses are ever logged or put into an error message.

Optional ``site.json`` keys (a missing key means the behaviour above): ``redirect_param`` (sent as
``redirect_uri`` instead, e.g. an eBay RuName; the pasted address still matches ``redirect_uri``),
``token_auth`` (``body`` | ``basic``: HTTP Basic header, no client id/secret in the form), ``pkce``
(default true) and ``refresh_scope`` (send ``scopes`` on refresh).
"""
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import sites

GOOGLE = {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "extra_params": "access_type=offline&prompt=consent",
    "redirect_uri": "http://localhost",
}

PENDING_TTL_S = 600
REFRESH_MARGIN_S = 300
TIMEOUT_S = 20
MAX_BYTES = 200_000
_DEFAULT_PORT = {"http": 80, "https": 443}
_CODE = re.compile(r"[A-Za-z0-9_.-]{1,40}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _epoch(iso) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _write_private(path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data).encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _site(site_id) -> dict:
    """site.json of an oauth site with its required fields; ValueError otherwise."""
    try:
        meta = sites._read(site_id)
    except KeyError:
        raise ValueError("unknown site") from None
    if meta.get("kind") != "oauth":
        raise ValueError("not an oauth site")
    for key in ("auth_url", "token_url", "client_id", "redirect_uri"):
        if not isinstance(meta.get(key), str) or not meta[key]:
            raise ValueError(f"site has no {key}")
    return meta


def matches_redirect(url, redirect_uri) -> bool:
    """Scheme, host and port equal (a missing port means the scheme default); the path is free."""
    try:
        a, b = urlsplit(url), urlsplit(redirect_uri)
        if not a.scheme or not a.hostname or a.scheme.lower() != b.scheme.lower():
            return False
        if (a.hostname or "").lower() != (b.hostname or "").lower():
            return False
        default = _DEFAULT_PORT.get(a.scheme.lower())
        return (a.port or default) == (b.port or default)
    except (ValueError, AttributeError, TypeError):
        return False


def start(site_id, redirect_uri=None) -> dict:
    """Create state + PKCE, store ``pending.json`` and return the URL the user must open.

    ``redirect_uri`` overrides the site's (the host browser uses a one-shot loopback listener).
    """
    meta = _site(site_id)
    redirect = redirect_uri or meta.get("redirect_param") or meta["redirect_uri"]
    match_url = redirect_uri or meta["redirect_uri"]
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": meta["client_id"],
        "redirect_uri": redirect,
        "scope": meta.get("scopes") or "",
        "state": state,
    }
    pending = {"state": state, "redirect_uri": redirect, "sent_redirect": redirect, "match_url": match_url,
               "expires_at": time.time() + PENDING_TTL_S}
    if meta.get("pkce", True) is not False:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
        params.update(code_challenge=challenge.decode("ascii"), code_challenge_method="S256")
        pending["verifier"] = verifier
    url = meta["auth_url"] + ("&" if urlsplit(meta["auth_url"]).query else "?") + urlencode(params)
    if meta.get("extra_params"):
        url += "&" + meta["extra_params"]
    with sites._locked(site_id):
        _write_private(sites._dir(site_id) / "pending.json", pending)
    return {"auth_url": url, "redirect_uri": match_url}


def _post(token_url: str, form: dict, headers: dict | None = None):
    """POST a form. Returns (http status, parsed JSON dict). Raises OSError-like on network trouble."""
    req = Request(token_url, data=urlencode(form).encode("ascii"), method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", **(headers or {})})
    try:
        with build_opener(_NoRedirect).open(req, timeout=TIMEOUT_S) as resp:
            status, raw = resp.status, resp.read(MAX_BYTES)
    except HTTPError as e:
        status, raw = e.code, e.read(MAX_BYTES)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _client_auth(meta: dict, form: dict, client_secret):
    """Add the client credentials to ``form``, or return a Basic header (``token_auth: basic``).

    Raises ValueError("site has no client_secret") for basic without a stored secret."""
    if meta.get("token_auth") == "basic":
        if not client_secret:
            raise ValueError("site has no client_secret")
        cred = base64.b64encode(f"{meta['client_id']}:{client_secret}".encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + cred}
    form["client_id"] = meta["client_id"]
    if client_secret:
        form["client_secret"] = client_secret
    return None


def _safe_code(value) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else "unknown"


def _token_fields(body: dict, now: float) -> dict:
    """The secret fields a successful token response yields."""
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("token response has no access token")
    out = {"access_token": token}
    expires = body.get("expires_in")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        out["expires_at"] = _iso(now + expires)
    else:
        out["expires_at"] = _iso(now + 3600)
    if isinstance(body.get("refresh_token"), str) and body["refresh_token"]:
        out["refresh_token"] = body["refresh_token"]
    return out


def _set_status(site_id, status: str, detail) -> None:
    with sites._locked(site_id):
        meta = sites._read(site_id)
        meta.update(status=status, checked_at=sites._now(), detail=detail)
        if status == "ok":
            meta.update(misses=0, notified=False)
        sites._store(site_id, meta)


def finish(site_id, redirect_url) -> None:
    """Exchange the code in the address the user pasted. ValueError (no secrets in it) on failure."""
    meta = _site(site_id)
    if not isinstance(redirect_url, str) or len(redirect_url) > 4096:
        raise ValueError("bad address")
    redirect_url = redirect_url.strip()
    try:
        pending = json.loads((sites._dir(site_id) / "pending.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = None
    expected = (pending.get("match_url") or pending.get("redirect_uri")) if isinstance(pending, dict) else None
    if not matches_redirect(redirect_url, expected or meta["redirect_uri"]):
        raise ValueError("that is not the page the login ended on")
    q = parse_qs(urlsplit(redirect_url).query)
    err = q.get("error", [None])[0]
    if err:
        raise ValueError(f"sign-in refused: {_safe_code(err)}")
    code, state = q.get("code", [None])[0], q.get("state", [None])[0]
    if pending is None:
        raise ValueError("no sign-in in progress, start again")
    if not isinstance(pending, dict) or time.time() > float(pending.get("expires_at") or 0):
        raise ValueError("sign-in expired, start again")
    if not code or not state or not secrets.compare_digest(str(state), str(pending.get("state"))):
        raise ValueError("sign-in did not match, start again")
    form = {"code": code, "redirect_uri": pending.get("sent_redirect") or pending["redirect_uri"],
            "grant_type": "authorization_code"}
    if pending.get("verifier") and meta.get("pkce", True) is not False:
        form["code_verifier"] = pending["verifier"]
    headers = _client_auth(meta, form, sites.read_secret(site_id).get("client_secret"))
    now = time.time()
    try:
        status, body = _post(meta["token_url"], form, headers)
    except (URLError, OSError, ValueError):
        raise ValueError("token request failed (network)") from None
    if status != 200:
        raise ValueError(f"token request refused: {_safe_code(body.get('error'))} (HTTP {status})")
    fields = _token_fields(body, now)
    if "refresh_token" not in fields:
        raise ValueError("no refresh token received (offline access missing?)")
    sites.write_secret(site_id, fields)
    with sites._locked(site_id):
        (sites._dir(site_id) / "pending.json").unlink(missing_ok=True)
    _set_status(site_id, "ok", None)


def refresh(site_id) -> str:
    """Get a new access token. Returns ``ok``, ``expired`` (invalid_grant) or ``error``."""
    meta = _site(site_id)
    secret = sites.read_secret(site_id)
    refresh_token = secret.get("refresh_token")
    if not refresh_token:
        _set_status(site_id, "expired", "not signed in")
        return "expired"
    form = {"refresh_token": refresh_token, "grant_type": "refresh_token"}
    if meta.get("refresh_scope") is True and meta.get("scopes"):
        form["scope"] = meta["scopes"]
    try:
        headers = _client_auth(meta, form, secret.get("client_secret"))
    except ValueError:
        _set_status(site_id, "error", "no client_secret")
        return "error"
    now = time.time()
    try:  # HTTP outside the lock
        status, body = _post(meta["token_url"], form, headers)
    except (TimeoutError, socket.timeout):
        _set_status(site_id, "error", "timeout")
        return "error"
    except (URLError, OSError, ValueError):
        _set_status(site_id, "error", "network error")
        return "error"
    if status == 200:
        try:
            fields = _token_fields(body, now)
        except ValueError:
            _set_status(site_id, "error", "bad token response")
            return "error"
        sites.write_secret(site_id, fields)  # merges, so a rotated refresh_token replaces the old one
        _set_status(site_id, "ok", None)
        return "ok"
    if status in (400, 401) and body.get("error") == "invalid_grant":
        _set_status(site_id, "expired", "invalid_grant")
        return "expired"
    _set_status(site_id, "error", f"HTTP {status}" if status >= 500 else _safe_code(body.get("error")))
    return "error"


def access_token(site_id) -> str:
    """A valid access token; refreshes first when fewer than 5 minutes are left."""
    _site(site_id)
    secret = sites.read_secret(site_id)
    token = secret.get("access_token")
    if not token or _epoch(secret.get("expires_at")) - time.time() < REFRESH_MARGIN_S:
        if refresh(site_id) != "ok":
            raise ValueError("oauth login not available, sign in again")
        token = sites.read_secret(site_id).get("access_token")
    return token
