"""Site logins: a cookie session per site, kept alive from the host.

The app logs in inside a web view and uploads the cookies once (``save_session``). From then on only
the host uses them: ``check`` fetches ``check_url`` with the jar and the stored user agent, and the
site counts as logged in while the body contains ``marker``. ``keepalive`` repeats that every six
hours and publishes one notice when a login expired. Skills read the jar through ``cookie_file``.

Files live in ``<data dir>/sites/<id>/`` (``site.json``, ``cookies.txt``, ``.lock``), mode 0600/0700.
Cookie values are never logged and no function here returns them except ``cookie_file``.
"""
import asyncio
import contextlib
import fcntl
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
from urllib.request import HTTPCookieProcessor, Request, build_opener

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


def _view(meta: dict) -> dict:
    return {
        "id": meta["id"],
        "name": meta["name"],
        "login_url": meta["login_url"],
        "check_domain": _registrable(_host(meta["check_url"])),
        "status": meta.get("status", "missing"),
        "checked_at": meta.get("checked_at"),
        "saved_at": meta.get("saved_at"),
        "detail": meta.get("detail"),
    }


def _url(value, what: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or _CTRL.search(value):
        raise ValueError(f"bad {what}")
    parts = urlsplit(value)
    ok_scheme = parts.scheme == "https" or (_ALLOW_HTTP and parts.scheme == "http")
    if not ok_scheme or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{what} must be an https:// URL")
    return value


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


def upsert(site_id, name, login_url, check_url, marker) -> dict:
    """Add or change a site definition. A new ``check_url`` host drops the stored session."""
    _dir(site_id)
    if not isinstance(name, str) or not name.strip() or len(name) > 64 or _CTRL.search(name):
        raise ValueError("bad name")
    if not isinstance(marker, str) or not marker or len(marker) > 200 or _CTRL.search(marker):
        raise ValueError("bad marker")
    _url(login_url, "login_url")
    _url(check_url, "check_url")
    with _locked(site_id):
        try:
            meta = _read(site_id)
        except KeyError:
            meta = None
        if meta is not None and _host(meta["check_url"]) != _host(check_url):
            (_dir(site_id) / "cookies.txt").unlink(missing_ok=True)
            meta = None
        fresh = {
            "status": "missing", "checked_at": None, "saved_at": None, "detail": None,
            "misses": 0, "notified": False, "user_agent": None,
        }
        meta = {**(meta or fresh), "id": site_id, "name": name.strip(), "login_url": login_url,
                "check_url": check_url, "marker": marker}
        _store(site_id, meta)
        return _view(meta)


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


def check(site_id) -> dict:
    """Fetch ``check_url`` with the stored session and update the status. Blocking."""
    meta = _read(site_id)
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
        if view["status"] not in ("ok", "error") or not (_dir(view["id"]) / "cookies.txt").exists():
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
        print(f"{v['id']}\t{v['status']}\t{v['name']}\t{v['check_domain']}")
    return 0


def _cli_add(args) -> int:
    try:
        v = upsert(args.id, args.name, args.login_url, args.check_url, args.marker)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{v['id']}\t{v['status']}\t{v['name']}")
    return 0


def _cli_path(args) -> int:
    try:
        print(stored_cookie_path(args.id))
    except KeyError:
        print("error: unknown site", file=sys.stderr)
        return 1
    return 0


def setup_cli(sub) -> None:
    """Add ``sites list | add | path`` to the ``hermes app`` subparsers."""
    sites = sub.add_parser("sites", help="Sites the app can log in to (list, add, path)")
    ssub = sites.add_subparsers(dest="sites_command")
    sites.set_defaults(func=lambda args: sites.print_help() or 0)
    p = ssub.add_parser("list", help="List sites and their login status")
    p.set_defaults(func=_cli_list)
    p = ssub.add_parser("add", help="Add or change a site")
    p.add_argument("id", help="lowercase letters, digits and dashes")
    p.add_argument("--name", required=True)
    p.add_argument("--login-url", required=True, help="https page the app opens for the login")
    p.add_argument("--check-url", required=True, help="https page fetched to test the login")
    p.add_argument("--marker", required=True, help="text that appears on check-url only when logged in")
    p.set_defaults(func=_cli_add)
    p = ssub.add_parser("path", help="Print the stored cookie file (Netscape format)")
    p.add_argument("id")
    p.set_defaults(func=_cli_path)
