"""HTTP routes of the hermes_app plugin, served on the API server's own port under ``/app/v1``.

``register(ctx)`` hands ``wire`` to ``ctx.register_platform_handler("api_server", ...)``; the API
server calls it with its aiohttp ``web.Application`` before the router freezes.

Auth: the API server has no app-wide auth middleware (each core handler calls ``_check_auth``),
so every route here is wrapped with the same check. ``adapter._check_auth`` is used when present
(profile aware); otherwise the bearer token is compared with the adapter's ``API_SERVER_KEY``.
Without a usable key every request is refused.
"""
import asyncio
import hmac
import logging
from typing import Optional

from aiohttp import web

from . import host_browser, search, site_oauth, site_providers, sites, store, todos, upload

logger = logging.getLogger(__name__)

PLUGIN = "hermes_app"
VERSION = "2.3.0"
FEATURES = ["notices", "devices", "upload", "search", "todos", "sites", "site_manage", "site_providers"]
PREFIX = "/app/v1"
KEEPALIVE_KEY = web.AppKey("hermes_app_sites_keepalive", asyncio.Task)  # the keepalive task
WIRED_KEY = web.AppKey("hermes_app_wired", bool)  # once-per-app guard


def _unauthorized() -> web.Response:
    return web.json_response({"error": "unauthorized"}, status=401)


def _check(adapter, request: web.Request) -> Optional[web.Response]:
    """None when the request carries the API server key, else a 401."""
    check = getattr(adapter, "_check_auth", None)
    if callable(check):
        return check(request)
    key = getattr(adapter, "_api_key", "") or ""
    header = request.headers.get("Authorization", "")
    if key and header.startswith("Bearer "):
        if hmac.compare_digest(header[7:].strip().encode(), key.encode()):
            return None
    return _unauthorized()


def _authed(adapter, handler):
    async def wrapper(request: web.Request) -> web.Response:
        denied = _check(adapter, request)
        if denied is not None:
            return denied
        return await handler(request)

    return wrapper


async def capabilities(request: web.Request) -> web.Response:
    features = FEATURES + (["host_login"] if host_browser.available() else [])
    return web.json_response({"plugin": PLUGIN, "version": VERSION, "features": features})


async def notices(request: web.Request) -> web.Response:
    try:
        limit = max(1, int(request.query.get("limit", 100)))
    except ValueError:
        limit = 100
    return web.json_response({"notices": store.latest(limit)})


async def devices(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        token = str(body["token"]).strip()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    environment = body.get("environment") or "production"
    if not token or environment not in ("production", "sandbox"):
        return web.json_response({"error": "bad request"}, status=400)
    kind = "widget" if body.get("kind") == "widget" else "alert"
    store.upsert_device(token, environment, kind)
    return web.json_response({"ok": True})


async def upload_file(request: web.Request) -> web.Response:
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return web.json_response({"error": "payload too large"}, status=413)
    if not body:
        return web.json_response({"error": "empty body"}, status=400)
    if len(body) > upload.MAX_BYTES:
        return web.json_response({"error": "payload too large"}, status=413)
    mime = request.content_type or "application/octet-stream"
    try:
        saved = await asyncio.to_thread(upload.save, body, request.headers.get("X-Filename", ""), mime)
    except OSError:
        logger.exception("hermes_app: could not store upload")
        return web.json_response({"error": "save failed"}, status=500)
    return web.json_response(saved, status=201)


async def search_messages(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", search.DEFAULT_LIMIT))
    except ValueError:
        limit = search.DEFAULT_LIMIT
    hits = await asyncio.to_thread(search.query, request.query.get("q", ""), limit)
    return web.json_response({"hits": hits})


async def get_todos(request: web.Request) -> web.Response:
    session_id = request.query.get("session_id", "").strip()
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)
    plan = todos.latest(session_id)
    if plan is None:
        return web.json_response({"error": "no plan"}, status=404)
    return web.json_response({"session_id": session_id, "todos": plan})


async def get_sites(request: web.Request) -> web.Response:
    return web.json_response({"sites": await asyncio.to_thread(sites.list_sites)})


async def put_site_session(request: web.Request) -> web.Response:
    site_id = request.match_info["id"]
    try:
        body = await request.json()
        cookies, user_agent = body["cookies"], body["user_agent"]
        if not isinstance(cookies, list) or not isinstance(user_agent, str):
            raise ValueError
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    try:
        await asyncio.to_thread(sites.save_session, site_id, cookies, user_agent)
    except KeyError:
        return web.json_response({"error": "unknown site"}, status=404)
    except ValueError:
        return web.json_response({"error": "bad request"}, status=400)
    return web.json_response(await asyncio.to_thread(sites.check, site_id))


async def check_site(request: web.Request) -> web.Response:
    try:
        view = await asyncio.to_thread(sites.check, request.match_info["id"])
    except KeyError:
        return web.json_response({"error": "unknown site"}, status=404)
    return web.json_response(view)


def _bad() -> web.Response:
    return web.json_response({"error": "bad request"}, status=400)


def _unknown() -> web.Response:
    return web.json_response({"error": "unknown site"}, status=404)


async def _body(request: web.Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError
    return body


async def _view(site_id: str) -> Optional[dict]:
    return await asyncio.to_thread(sites.get, site_id)


async def post_site(request: web.Request) -> web.Response:
    try:
        fields = await _body(request)
    except Exception:
        return _bad()
    try:
        view = await asyncio.to_thread(sites.put, fields)
    except ValueError:
        return _bad()
    return web.json_response(view)


async def get_site_providers(request: web.Request) -> web.Response:
    return web.json_response({"providers": site_providers.public_list()})


async def delete_site(request: web.Request) -> web.Response:
    if not await asyncio.to_thread(sites.remove, request.match_info["id"]):
        return _unknown()
    return web.json_response({"ok": True})


async def put_api_key(request: web.Request) -> web.Response:
    try:
        key = (await _body(request))["key"]
        if not isinstance(key, str):
            raise ValueError
    except Exception:
        return _bad()
    try:
        await asyncio.to_thread(sites.set_api_key, request.match_info["id"], key)
        view = await asyncio.to_thread(sites.check, request.match_info["id"])
    except KeyError:
        return _unknown()
    except ValueError:
        return _bad()
    return web.json_response(view)


async def oauth_start(request: web.Request) -> web.Response:
    try:
        started = await asyncio.to_thread(site_oauth.start, request.match_info["id"])
    except KeyError:
        return _unknown()
    except ValueError:
        return _bad()
    return web.json_response({"auth_url": started["auth_url"], "redirect_uri": started["redirect_uri"]})


async def oauth_finish(request: web.Request) -> web.Response:
    site_id = request.match_info["id"]
    try:
        redirect_url = (await _body(request))["redirect_url"]
        if not isinstance(redirect_url, str):
            raise ValueError
    except Exception:
        return _bad()
    try:
        await asyncio.to_thread(site_oauth.finish, site_id, redirect_url)
    except KeyError:
        return _unknown()
    except ValueError:
        return web.json_response({"error": "login failed"}, status=400)
    view = await _view(site_id)
    return web.json_response(view) if view is not None else _unknown()


LISTENER_TTL = 900
_LISTENER_HTML = "<!doctype html><meta charset=utf-8><title>Hermes</title><p>{}</p>"
# site_id -> {"server": asyncio.Server, "ok": Optional[bool], "timer": TimerHandle}
_listeners: dict = {}


def _stop_listener(site_id: str) -> None:
    """Close the one-shot loopback listener and forget its result."""
    entry = _listeners.pop(site_id, None)
    if entry is not None:
        entry["server"].close()
        entry["timer"].cancel()


def _stop_all_listeners() -> None:
    for site_id in list(_listeners):
        _stop_listener(site_id)


async def _open_listener(site_id: str) -> str:
    """Listen once on 127.0.0.1 (random port) for the OAuth redirect and finish the login with it.

    Returns the redirect URI. The result (True/False) is kept in ``_listeners[site_id]["ok"]``.
    """
    _stop_listener(site_id)
    entry = {"server": None, "ok": None, "timer": None}

    async def respond(writer, status: str, text: str) -> None:
        body = _LISTENER_HTML.format(text).encode()
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def handle(reader, writer) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            parts = line.decode("latin-1").split()
            target = parts[1] if len(parts) >= 2 and parts[1].startswith("/") else None
            if target is None or entry["ok"] is not None or not ("code=" in target or "error=" in target):
                await respond(writer, "404 Not Found", "Not found")  # favicon and the like
                return
            entry["ok"] = False  # claim the one shot before the slow token request
            url = f"http://127.0.0.1:{port}{target}"
            try:
                await asyncio.to_thread(site_oauth.finish, site_id, url)
                entry["ok"] = True
            except Exception:
                logger.warning("host login redirect for site %s did not finish", site_id)
            if entry["ok"]:
                await respond(writer, "200 OK", "Signed in. You can close this window.")
            else:
                await respond(writer, "200 OK", "Sign-in failed. Close this window and try again.")
            entry["server"].close()
        except Exception:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    entry["server"] = server
    entry["timer"] = asyncio.get_running_loop().call_later(LISTENER_TTL, _stop_listener, site_id)
    _listeners[site_id] = entry
    return f"http://127.0.0.1:{port}/"


async def host_login(request: web.Request) -> web.Response:
    site_id = request.match_info["id"]
    view = await _view(site_id)
    if view is None:
        return _unknown()
    kind = view.get("kind") or "cookie"
    if kind not in ("cookie", "oauth"):
        return _bad()
    if kind == "oauth" and view.get("redirect_param"):  # its redirect is fixed: no loopback listener
        return web.json_response({"error": "sign in with Safari for this site"}, status=400)
    if not host_browser.available():
        return web.json_response({"error": "host login unavailable"}, status=501)
    try:
        if kind == "oauth":
            redirect = await _open_listener(site_id)
            url = (await asyncio.to_thread(site_oauth.start, site_id, redirect))["auth_url"]
        else:
            url = view.get("login_url")
        if not url:
            return _bad()
        await host_browser.start(site_id, url)
    except KeyError:
        _stop_listener(site_id)
        return _unknown()
    except ValueError:
        _stop_listener(site_id)
        return _bad()
    except BaseException:
        _stop_listener(site_id)
        raise
    return web.json_response({"ok": True})


async def host_login_finish(request: web.Request) -> web.Response:
    site_id = request.match_info["id"]
    try:
        save = bool((await _body(request)).get("save"))
    except Exception:
        return _bad()
    view = await _view(site_id)
    if view is None:
        return _unknown()
    oauth = (view.get("kind") or "cookie") == "oauth"
    try:
        if save:
            if oauth:
                entry = _listeners.get(site_id)
                if entry is None or entry["ok"] is not True:
                    return web.json_response({"error": "login not finished"}, status=400)
            else:
                try:
                    cookies = await host_browser.cookies(site_id)
                    user_agent = await host_browser.user_agent(site_id)
                except RuntimeError:
                    return web.json_response({"error": "host login failed"}, status=500)
                try:
                    await asyncio.to_thread(sites.save_session, site_id, cookies, user_agent)
                except ValueError:
                    return web.json_response({"error": "login not finished"}, status=400)
                await asyncio.to_thread(sites.check, site_id)
    except KeyError:
        return _unknown()
    finally:
        _stop_listener(site_id)
        await host_browser.close(site_id)
    view = await _view(site_id)
    return web.json_response(view) if view is not None else _unknown()


def _start_keepalive(publish):
    async def start(app: web.Application) -> None:
        app[KEEPALIVE_KEY] = asyncio.create_task(sites.keepalive(publish))

    async def stop(app: web.Application) -> None:
        task = app.get(KEEPALIVE_KEY)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _stop_all_listeners()
        await host_browser.close_all()

    return start, stop


def wire(app: web.Application, adapter) -> None:
    """Add the ``/app/v1`` routes to the API server's application."""
    from .adapter import publish  # lazy: adapter imports this module

    for method, path, handler in (
        ("GET", "/capabilities", capabilities),
        ("GET", "/notices", notices),
        ("POST", "/devices", devices),
        ("POST", "/upload", upload_file),
        ("GET", "/search", search_messages),
        ("GET", "/todos", get_todos),
        ("GET", "/sites", get_sites),
        ("GET", "/sites/providers", get_site_providers),
        ("PUT", "/sites/{id}/session", put_site_session),
        ("POST", "/sites/{id}/check", check_site),
        ("POST", "/sites", post_site),
        ("DELETE", "/sites/{id}", delete_site),
        ("PUT", "/sites/{id}/api-key", put_api_key),
        ("POST", "/sites/{id}/oauth/start", oauth_start),
        ("POST", "/sites/{id}/oauth/finish", oauth_finish),
        ("POST", "/sites/{id}/host-login", host_login),
        ("POST", "/sites/{id}/host-login/finish", host_login_finish),
    ):
        app.router.add_route(method, PREFIX + path, _authed(adapter, handler))
    if WIRED_KEY not in app:
        app[WIRED_KEY] = True
        start, stop = _start_keepalive(publish)
        app.on_startup.append(start)
        app.on_cleanup.append(stop)


def register(ctx) -> None:
    ctx.register_platform_handler("api_server", wire)
