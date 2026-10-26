"""Site logins, three kinds per site (``kind`` in site.json), kept alive from the host.

``cookie``: the app logs in inside a web view and uploads the cookies once (``save_session``). From
then on only the host uses them: ``check`` fetches ``check_url`` with the jar and the stored user
agent, and the site counts as logged in while the body contains ``marker``. Skills read the jar
through ``cookie_file``. ``api_key``: a key in ``secret.json``, checked with ``check_url`` (optional)
without following redirects. ``oauth``: authorization code + PKCE, tokens in ``secret.json``,
handled by ``site_oauth``. ``keepalive`` checks due sites every six hours and publishes one notice
when a login expired.

Files live in ``<data dir>/sites/<id>/`` (``site.json``, ``secret.json``, ``cookies.txt``,
``pending.json``, ``.lock``), mode 0600/0700. Secret values (cookies, keys, tokens, client secrets,
URLs with codes) are never logged and no view contains them; only ``cookie_file``, ``read_secret``
and ``secret`` return them.
"""
import asyncio
import contextlib
import fcntl
import getpass
import importlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .store import _data_dir

log = logging.getLogger(__name__)

INTERVAL_S = 6 * 3600  # between two checks of one site
POLL_S = 15 * 60  # how often the loop looks for due sites
TIMEOUT_S = 20
MAX_BYTES = 2_000_000
MAX_COOKIES = 500
EXPIRE_AFTER_MISSES = 2
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_HEADER = re.compile(r"[A-Za-z0-9-]{1,64}")
_KINDS = ("cookie", "api_key", "oauth")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_ALLOW_HTTP = False  # tests only: lets check_url point at a local http server


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- paths and files -------------------------------------------------------------------------


def _dir(site_id) -> Path:
    if not isinstance(site_id, str) or not _ID.fullmatch(site_id):
        raise ValueError("bad site id")
    return _data_dir() / "sites" / site_id


def _write(path: Path, data: bytes) -> None:
    """Atomic write with mode 0600."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_jar(jar: MozillaCookieJar, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    os.close(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _mkdir(d: Path) -> None:
    d.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    d.mkdir(exist_ok=True, mode=0o700)


@contextlib.contextmanager
def _locked(site_id: str):
    """Exclusive lock per site; flock works across threads and processes. Not reentrant."""
    d = _dir(site_id)
    _mkdir(d)
    fd = os.open(d / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _read(site_id: str) -> dict:
    """site.json of a known site. KeyError for an invalid or unknown id."""
    try:
        path = _dir(site_id) / "site.json"
    except ValueError:
        raise KeyError(site_id) from None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise KeyError(site_id) from None
    if not isinstance(meta, dict):
        raise KeyError(site_id)
    return meta


def _store(site_id: str, meta: dict) -> None:
    _write(_dir(site_id) / "site.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))


def read_secret(site_id: str) -> dict:
    """secret.json of a site ({} when there is none). Atomic file, so no lock needed."""
    try:
        data = json.loads((_dir(site_id) / "secret.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_secret(site_id: str, data: dict) -> None:
    """Merge into secret.json (a None value deletes the key). Call inside ``_locked``."""
    merged = {**read_secret(site_id), **data}
    merged = {k: v for k, v in merged.items() if v is not None}
    _write(_dir(site_id) / "secret.json", json.dumps(merged, ensure_ascii=False).encode("utf-8"))


def write_secret(site_id: str, data: dict) -> None:
    """Merge ``data`` into secret.json under the site lock. KeyError for an unknown site."""
    _read(site_id)
    with _locked(site_id):
        _merge_secret(site_id, data)


# --- domains and views -----------------------------------------------------------------------


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _registrable(host: str) -> str:
    """Last two labels of the host (the host itself for an IP or a single label)."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return ".".join(host.split(".")[-2:])


def _domain_ok(domain: str, allowed: set) -> bool:
    d = domain.lower().lstrip(".")
    return any(d == a or d.endswith("." + a) for a in allowed)


def _kind(meta: dict) -> str:
    return meta.get("kind") or "cookie"


def _has_secret(meta: dict) -> tuple[bool, bool]:
    """(site has its credential, client secret stored). Tolerates a damaged directory."""
    try:
        d = _dir(meta.get("id"))
        if _kind(meta) == "cookie":
            return (d / "cookies.txt").exists(), False
        secret = read_secret(meta["id"])
    except (ValueError, KeyError, OSError):
        return False, False
    has = _api_key(secret) if _kind(meta) == "api_key" else secret.get("refresh_token")
    return bool(has), bool(secret.get("client_secret"))


def _view(meta: dict) -> dict:
    kind = _kind(meta)
    check_url = meta.get("check_url")
    has_secret, has_client_secret = _has_secret(meta)
    view = {
        "id": meta["id"],
        "name": meta["name"],
        "kind": kind,
        "login_url": meta.get("login_url"),
        "check_domain": _registrable(_host(check_url)) if check_url else None,
        "has_secret": has_secret,
        "status": meta.get("status", "missing"),
        "checked_at": meta.get("checked_at"),
        "saved_at": meta.get("saved_at"),
        "detail": meta.get("detail"),
    }
    if kind == "cookie":
        view.update(check_url=check_url, marker=meta.get("marker"))
    elif kind == "api_key":
        view.update(check_url=check_url, header=meta.get("header"), prefix=meta.get("prefix"))
    else:
        view.update({k: meta.get(k) for k in ("auth_url", "token_url", "client_id", "scopes", "redirect_uri")})
        view["has_client_secret"] = has_client_secret
    return view


def _url(value, what: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or _CTRL.search(value):
        raise ValueError(f"bad {what}")
    parts = urlsplit(value)
    ok_scheme = parts.scheme == "https" or (_ALLOW_HTTP and parts.scheme == "http")
    if not ok_scheme or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{what} must be an https:// URL")
    return value


def _redirect_uri(value) -> str:
    """https, or http to a loopback host (the OAuth redirect lands on the user's own device)."""
    if not isinstance(value, str) or len(value) > 2048 or _CTRL.search(value):
        raise ValueError("bad redirect_uri")
    parts = urlsplit(value)
    loopback = parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK
    if not (parts.scheme == "https" or loopback) or not parts.hostname or parts.username or parts.password:
        raise ValueError("redirect_uri must be https:// or http://localhost")
    return value


def _text(value, what: str, max_len: int, required: bool = True):
    if value is None:
        if required:
            raise ValueError(f"bad {what}")
        return None
    if not isinstance(value, str) or (required and not value) or len(value) > max_len or _CTRL.search(value):
        raise ValueError(f"bad {what}")
    return value


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-")


# --- public api ------------------------------------------------------------------------------


def list_sites() -> list[dict]:
    root = _data_dir() / "sites"
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        try:
            out.append(_view(_read(d.name)))
        except (KeyError, TypeError):
            continue
    return sorted(out, key=lambda v: v["name"].lower())


def get(site_id) -> dict | None:
    try:
        return _view(_read(site_id))
    except KeyError:
        return None


def _oauth_module():
    # lazy: site_oauth imports this module
    return importlib.import_module(f"{__package__}.site_oauth")


def _fields_for(kind: str, fields: dict, base: dict, preset) -> dict:
    """The validated, kind specific part of site.json. Missing field = keep ``base``, "" = clear."""

    def get(key, default=None):
        v = fields.get(key)
        if v is None:
            return base.get(key, default)
        return None if v == "" else v

    if kind == "cookie":
        marker = get("marker")
        if not isinstance(marker, str) or not marker or len(marker) > 200 or _CTRL.search(marker):
            raise ValueError("bad marker")
        return {"login_url": _url(get("login_url"), "login_url"),
                "check_url": _url(get("check_url"), "check_url"), "marker": marker}
    if kind == "api_key":
        check_url = get("check_url")
        header, prefix = get("header", "Authorization"), fields.get("prefix")
        if prefix is None:
            prefix = base.get("prefix", "Bearer ")
        if not isinstance(header, str) or not _HEADER.fullmatch(header):
            raise ValueError("bad header")
        return {"check_url": None if check_url is None else _url(check_url, "check_url"),
                "header": header, "prefix": _text(prefix, "prefix", 64, required=False)}
    out = {}
    if preset:
        google = _oauth_module().GOOGLE
        out["extra_params"] = google["extra_params"]
        for key in ("auth_url", "token_url", "redirect_uri"):  # explicit fields (e.g. a client file) win
            if fields.get(key) in (None, ""):
                fields = {**fields, key: google[key]}
    else:
        out["extra_params"] = base.get("extra_params")  # never from the request: presets and CLI only
    scopes = get("scopes")
    if isinstance(scopes, list) and all(isinstance(x, str) for x in scopes):
        scopes = " ".join(scopes)
    out.update(
        auth_url=_url(get("auth_url"), "auth_url"), token_url=_url(get("token_url"), "token_url"),
        client_id=_text(get("client_id"), "client_id", 256),
        scopes=_text(scopes, "scopes", 1024, required=False),
        redirect_uri=_redirect_uri(get("redirect_uri")))
    _text(out["extra_params"], "extra_params", 512, required=False)
    return out


def _trust_host(meta: dict) -> str:
    """Host that receives the credential: check_url, or token_url for oauth."""
    return _host(meta.get("token_url" if _kind(meta) == "oauth" else "check_url") or "")


def put(fields: dict) -> dict:
    """Add or change a site (any kind). ValueError for bad input. Adding an existing id updates it.

    Changing the kind, or the host of ``check_url``/``token_url``, drops the stored credentials.
    ``client_secret`` goes to secret.json only; ``extra_params`` only comes from a preset.
    """
    if not isinstance(fields, dict):
        raise ValueError("bad site")
    name = fields.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 64 or _CTRL.search(name):
        raise ValueError("bad name")
    site_id = fields.get("id") or _slug(name)
    _dir(site_id)
    preset = fields.get("preset") or None
    if preset not in (None, "google"):
        raise ValueError("bad preset")
    try:
        existing = _read(site_id)
    except KeyError:
        existing = None
    kind = fields.get("kind") or ("oauth" if preset else _kind(existing or {}))
    kind = "api_key" if kind == "api-key" else kind
    if kind not in _KINDS or (preset and kind != "oauth"):
        raise ValueError("bad kind")
    client_secret = fields.get("client_secret")
    if client_secret == "":
        client_secret = None
    if client_secret is not None and (kind != "oauth" or not _text(client_secret, "client_secret", 1024)):
        raise ValueError("bad client_secret")
    base = existing if existing is not None and _kind(existing) == kind else {}
    new = {"id": site_id, "name": name.strip(), "kind": kind, **_fields_for(kind, fields, base, preset)}
    with _locked(site_id):
        try:
            meta = _read(site_id)
        except KeyError:
            meta = None
        if meta is not None and (_kind(meta) != kind or (_trust_host(meta) and _trust_host(meta) != _trust_host(new))):
            for fname in ("secret.json", "cookies.txt", "pending.json"):
                (_dir(site_id) / fname).unlink(missing_ok=True)
            meta = None
        fresh = {
            "status": "missing", "checked_at": None, "saved_at": None, "detail": None,
            "misses": 0, "notified": False, "user_agent": None,
        }
        meta = {**(meta or fresh), **new}
        _store(site_id, meta)
        if client_secret is not None:
            _merge_secret(site_id, {"client_secret": client_secret})
        return _view(meta)


def upsert(site_id, name, login_url, check_url, marker) -> dict:
    """Add or change a cookie site (0007 signature)."""
    _dir(site_id)
    return put({"id": site_id, "name": name, "kind": "cookie", "login_url": login_url,
                "check_url": check_url, "marker": marker})


def remove(site_id) -> bool:
    """Delete a site with all its files. False when it does not exist."""
    try:
        _read(site_id)
    except KeyError:
        return False
    with _locked(site_id):
        shutil.rmtree(_dir(site_id), ignore_errors=True)
    return True


def set_api_key(site_id, key) -> dict:
    """Store the key of an api_key site. KeyError for an unknown site, ValueError for bad input."""
    meta = _read(site_id)
    if _kind(meta) != "api_key":
        raise ValueError("not an api key site")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("bad key")
    key = key.strip()
    if len(key) > 4096 or _CTRL.search(key) or not key.isascii():
        raise ValueError("bad key")
    with _locked(site_id):
        meta = _read(site_id)
        _merge_secret(site_id, {"api_key": key, "key": None})
        unchecked = not meta.get("check_url")
        meta.update(saved_at=_now(), checked_at=_now() if unchecked else None,
                    status="ok" if unchecked else "error", detail="not checked",
                    misses=0, notified=False)
        _store(site_id, meta)
        return _view(meta)


def secret(site_id) -> str:
    """The API key, or a valid OAuth access token. Sensitive: callers must not log it."""
    meta = _read(site_id)
    kind = _kind(meta)
    if kind == "api_key":
        key = _api_key(read_secret(site_id))
        if not key:
            raise ValueError("no key set")
        return key
    if kind == "oauth":
        return _oauth_module().access_token(site_id)
    raise ValueError("cookie sites have no secret")


def _build_jar(cookies, allowed: set) -> MozillaCookieJar:
    jar = MozillaCookieJar()
    for c in cookies:
        if not isinstance(c, dict):
            raise ValueError("bad cookie")
        name, value, domain = c.get("name"), c.get("value"), c.get("domain")
        path, expires = c.get("path") or "/", c.get("expires")
        for s in (name, value, domain, path):
            if not isinstance(s, str) or _CTRL.search(s):
                raise ValueError("bad cookie")
        if not name or not domain or not path.startswith("/"):
            raise ValueError("bad cookie")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float))):
            raise ValueError("bad cookie")
        if not _domain_ok(domain, allowed):
            continue
        dotted = domain.startswith(".")
        jar.set_cookie(Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=dotted, domain_initial_dot=dotted,
            path=path, path_specified=True, secure=bool(c.get("secure")),
            expires=None if expires is None else int(expires), discard=expires is None,
            comment=None, comment_url=None, rest={},
        ))
    return jar


def save_session(site_id, cookies: list[dict], user_agent: str) -> None:
    """Store the app's cookies. KeyError for an unknown site, ValueError for bad input."""
    meta = _read(site_id)
    if _kind(meta) != "cookie":
        raise ValueError("not a cookie site")
    if not isinstance(cookies, list) or not cookies or len(cookies) > MAX_COOKIES:
        raise ValueError("bad cookies")
    if not isinstance(user_agent, str) or not user_agent.strip() or len(user_agent) > 512 or _CTRL.search(user_agent):
        raise ValueError("bad user_agent")
    allowed = {_registrable(_host(meta["login_url"])), _registrable(_host(meta["check_url"]))}
    jar = _build_jar(cookies, allowed)
    if not len(jar):
        raise ValueError("no cookies for this site")
    with _locked(site_id):
        meta = _read(site_id)
        _write_jar(jar, _dir(site_id) / "cookies.txt")
        meta.update(user_agent=user_agent.strip(), saved_at=_now(), checked_at=None, status="error",
                    detail="not checked", misses=0, notified=False)
        _store(site_id, meta)


def _fetch(url: str, user_agent: str, jar: MozillaCookieJar) -> bytes:
    opener = build_opener(HTTPCookieProcessor(jar))
    req = Request(url, headers={"User-Agent": user_agent, "Accept-Language": "en"})
    with opener.open(req, timeout=TIMEOUT_S) as resp:
        return resp.read(MAX_BYTES)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx surfaces as HTTPError, so the key is never sent anywhere else


def _fetch_key(url: str, header: str, value: str) -> None:
    opener = build_opener(_NoRedirect)
    req = Request(url, headers={header: value, "User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    with opener.open(req, timeout=TIMEOUT_S) as resp:
        resp.read(1)


def _api_key(secret: dict):
    """The stored key; ``key`` is the name used before 2.2.1."""
    return secret.get("api_key") or secret.get("key")


def _check_api_key(site_id: str, meta: dict) -> dict:
    key = _api_key(read_secret(site_id))
    if not key:
        return _view(meta)
    saved_at = meta.get("saved_at")
    outcome, detail = "error", None
    if not meta.get("check_url"):
        outcome, detail = "ok", "not checked"
    else:
        try:
            _fetch_key(meta["check_url"], meta.get("header") or "Authorization",
                       (meta.get("prefix") or "") + key)
            outcome = "ok"
        except HTTPError as e:
            if e.code in (401, 403):
                outcome, detail = "expired", f"HTTP {e.code}"
            elif 300 <= e.code < 400:
                detail = "redirect not followed"
            else:
                detail = f"HTTP {e.code}"
        except (TimeoutError, socket.timeout):
            detail = "timeout"
        except URLError as e:
            detail = "timeout" if isinstance(e.reason, (TimeoutError, socket.timeout)) else "network error"
        except Exception as e:
            detail = "network error" if isinstance(e, OSError) else "check failed"
            log.warning("site check %s: %s", site_id, type(e).__name__)
    with _locked(site_id):
        meta = _read(site_id)
        if meta.get("saved_at") != saved_at:  # a new key arrived meanwhile
            return _view(meta)
        meta.update(checked_at=_now(), status=outcome, detail=detail)
        if outcome == "ok":
            meta.update(misses=0, notified=False)
        _store(site_id, meta)
        return _view(meta)


def check(site_id) -> dict:
    """Test the stored credential and update the status. Blocking."""
    meta = _read(site_id)
    kind = _kind(meta)
    if kind == "api_key":
        return _check_api_key(site_id, meta)
    if kind == "oauth":
        if not _has_secret(meta)[0]:
            return _view(meta)
        _oauth_module().refresh(site_id)  # writes the status itself
        return _view(_read(site_id))
    return _check_cookie(site_id, meta)


def _check_cookie(site_id: str, meta: dict) -> dict:
    """Fetch ``check_url`` with the stored session and update the status."""
    cookies = _dir(site_id) / "cookies.txt"
    if not cookies.exists():
        return _view(meta)
    saved_at = meta.get("saved_at")
    jar = MozillaCookieJar()
    outcome, detail = "error", None
    try:
        jar.load(str(cookies), ignore_discard=True, ignore_expires=True)
        body = _fetch(meta["check_url"], meta.get("user_agent") or "Mozilla/5.0", jar)
        outcome = "ok" if meta["marker"].encode("utf-8") in body else "miss"
    except HTTPError as e:
        detail = f"HTTP {e.code}"
    except (TimeoutError, socket.timeout):
        detail = "timeout"
    except URLError as e:
        detail = "timeout" if isinstance(e.reason, (TimeoutError, socket.timeout)) else "network error"
    except Exception as e:  # unreadable jar, bad URL, dropped connection
        detail = "network error" if isinstance(e, OSError) else "check failed"
        log.warning("site check %s: %s", site_id, type(e).__name__)
    with _locked(site_id):
        meta = _read(site_id)
        if meta.get("saved_at") != saved_at:  # a new session arrived meanwhile; its check will follow
            return _view(meta)
        meta["checked_at"] = _now()
        if outcome == "ok":
            _write_jar(jar, cookies)
            meta.update(status="ok", detail=None, misses=0, notified=False)
        elif outcome == "miss":
            meta["misses"] = int(meta.get("misses") or 0) + 1
            if meta["misses"] >= EXPIRE_AFTER_MISSES:
                meta.update(status="expired", detail="marker missing")
            elif meta.get("status") == "error":
                meta["detail"] = "marker missing"
        else:
            meta.update(status="error", detail=detail)
        _store(site_id, meta)
        return _view(meta)


def cookie_file(site_id) -> Path:
    """A fresh 0600 temp copy of the jar (yt-dlp rewrites ``--cookies`` files). Caller deletes it."""
    _read(site_id)
    src = _dir(site_id) / "cookies.txt"
    if not src.exists():
        raise KeyError(site_id)
    fd, tmp = tempfile.mkstemp(prefix=f"hermes-site-{site_id}-", suffix=".txt")
    with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
        shutil.copyfileobj(f, out)
    return Path(tmp)


def stored_cookie_path(site_id) -> Path:
    _read(site_id)
    return _dir(site_id) / "cookies.txt"


def due_notices() -> list[dict]:
    """Notices for sites that are expired and not yet announced; marks them announced."""
    out = []
    for view in list_sites():
        sid = view["id"]
        if view["status"] != "expired":
            continue
        with _locked(sid):
            meta = _read(sid)
            if meta.get("status") != "expired" or meta.get("notified"):
                continue
            meta["notified"] = True
            _store(sid, meta)
        out.append({
            "id": uuid.uuid4().hex,
            "kind": "site",
            "title": f"Login expired: {meta['name']}",
            "body": f"Log in again: Settings → Sites → {meta['name']}.",
            "job_id": None,
            "created_at": _now(),
        })
    return out


def _due_ids() -> list[str]:
    now = datetime.now(timezone.utc)
    out = []
    for view in list_sites():
        # cookie: a jar; api_key: a key; oauth: a refresh token (has_secret). "missing" is not checked.
        if view["status"] not in ("ok", "error") or not view["has_secret"]:
            continue
        last = view["checked_at"]
        if last is None or (now - datetime.fromisoformat(last)).total_seconds() >= INTERVAL_S:
            out.append(view["id"])
    return out


async def keepalive(publish) -> None:
    """Check due sites and publish expiry notices, forever. ``publish`` is a sync callable(notice)."""
    while True:
        try:
            for sid in await asyncio.to_thread(_due_ids):
                try:
                    await asyncio.to_thread(check, sid)
                except KeyError:
                    pass
            for notice in await asyncio.to_thread(due_notices):
                publish(notice)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("sites keepalive: %s", type(e).__name__)
        await asyncio.sleep(POLL_S)


# --- CLI: hermes app sites ... ---------------------------------------------------------------


def _cli_list(args) -> int:
    for v in list_sites():
        print(f"{v['id']}\t{v['status']}\t{v['name']}\t{v['check_domain'] or '-'}\t{v['kind']}")
    return 0


def _client_json(path: str) -> dict:
    """client_id, client_secret and the first redirect URI of a Google OAuth client file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    client = data.get("installed") or data.get("web") if isinstance(data, dict) else None
    if not isinstance(client, dict) or not client.get("client_id"):
        raise ValueError("not a Google OAuth client file")
    out = {"client_id": client["client_id"], "client_secret": client.get("client_secret")}
    uris = client.get("redirect_uris")
    if isinstance(uris, list) and uris:
        out["redirect_uri"] = uris[0]
    return out


def _cli_add(args) -> int:
    kind = args.kind or ("oauth" if args.preset or args.client_json else None)
    fields = {"id": args.id, "name": args.name, "kind": kind, "preset": args.preset}
    for key in ("login_url", "check_url", "marker", "header", "prefix", "auth_url", "token_url",
                "client_id", "scopes", "redirect_uri"):
        fields[key] = getattr(args, key)
    try:
        if args.client_json:
            for key, value in _client_json(args.client_json).items():
                if fields.get(key) is None:
                    fields[key] = value
        elif kind == "oauth" and sys.stdin.isatty():
            fields["client_secret"] = getpass.getpass("client secret (Enter to skip): ") or None
        v = put({k: x for k, x in fields.items() if x is not None})
    except (ValueError, OSError) as e:  # messages never carry file contents or secrets
        print(f"error: {e if isinstance(e, ValueError) else 'cannot read the client file'}", file=sys.stderr)
        return 1
    print(f"{v['id']}\t{v['status']}\t{v['name']}\t{v['kind']}")
    return 0


def _cli_set_key(args) -> int:
    try:
        if args.from_file:
            key = Path(args.from_file).read_text(encoding="utf-8")
        elif sys.stdin.isatty():
            key = getpass.getpass("API key: ")
        else:
            key = sys.stdin.read()
        v = set_api_key(args.id, key)
    except KeyError:
        print("error: unknown site", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {e if isinstance(e, ValueError) else 'cannot read the key file'}", file=sys.stderr)
        return 1
    print(f"{v['id']}\t{v['status']}\t{v['name']}")
    return 0


def _cli_remove(args) -> int:
    if not remove(args.id):
        print("error: unknown site", file=sys.stderr)
        return 1
    return 0


def _cli_secret(args) -> int:
    try:
        print(secret(args.id))
    except KeyError:
        print("error: unknown site", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _cli_path(args) -> int:
    try:
        print(stored_cookie_path(args.id))
    except KeyError:
        print("error: unknown site", file=sys.stderr)
        return 1
    return 0


def setup_cli(sub) -> None:
    """Add ``sites list | add | set-key | remove | path | secret`` to the ``hermes app`` subparsers."""
    sites = sub.add_parser("sites", help="Sites the app can log in to (list, add, set-key, remove, path, secret)")
    ssub = sites.add_subparsers(dest="sites_command")
    sites.set_defaults(func=lambda args: sites.print_help() or 0)
    p = ssub.add_parser("list", help="List sites and their login status")
    p.set_defaults(func=_cli_list)
    p = ssub.add_parser("add", help="Add or change a site")
    p.add_argument("id", help="lowercase letters, digits and dashes")
    p.add_argument("--name", required=True)
    p.add_argument("--kind", choices=["cookie", "api-key", "oauth"],
                   help="default cookie (oauth with --preset or --client-json)")
    p.add_argument("--login-url", help="cookie: https page the app opens for the login")
    p.add_argument("--check-url", help="https page fetched to test the login (optional for api-key)")
    p.add_argument("--marker", help="cookie: text that appears on check-url only when logged in")
    p.add_argument("--header", help="api-key: header name (default Authorization)")
    p.add_argument("--prefix", help="api-key: value prefix (default 'Bearer ')")
    p.add_argument("--auth-url", help="oauth: authorization endpoint")
    p.add_argument("--token-url", help="oauth: token endpoint")
    p.add_argument("--client-id", help="oauth")
    p.add_argument("--scopes", help="oauth: space separated")
    p.add_argument("--redirect-uri", help="oauth: https or http://localhost")
    p.add_argument("--preset", choices=["google"], help="oauth: fill the Google endpoints")
    p.add_argument("--client-json", metavar="FILE",
                   help="oauth: Google client file (client_id, client_secret, redirect_uris[0]); "
                        "without it the client secret is asked on the terminal")
    p.set_defaults(func=_cli_add)
    p = ssub.add_parser("set-key", help="Set the key of an api-key site (from a file, else stdin; never argv)")
    p.add_argument("id")
    p.add_argument("--from-file", metavar="F")
    p.set_defaults(func=_cli_set_key)
    p = ssub.add_parser("remove", help="Delete a site and its stored credentials")
    p.add_argument("id")
    p.set_defaults(func=_cli_remove)
    p = ssub.add_parser("path", help="Print the stored cookie file (Netscape format)")
    p.add_argument("id")
    p.set_defaults(func=_cli_path)
    warn = "Print the API key or a fresh OAuth access token. SENSITIVE: do not log it or paste it anywhere"
    p = ssub.add_parser("secret", help=warn, description=warn)
    p.add_argument("id")
    p.set_defaults(func=_cli_secret)
