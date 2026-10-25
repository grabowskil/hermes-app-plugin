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

from . import search, store, todos, upload

logger = logging.getLogger(__name__)

PLUGIN = "hermes_app"
VERSION = "2.0.0"
FEATURES = ["notices", "devices", "upload", "search", "todos"]
PREFIX = "/app/v1"


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
    return web.json_response({"plugin": PLUGIN, "version": VERSION, "features": FEATURES})


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


def wire(app: web.Application, adapter) -> None:
    """Add the ``/app/v1`` routes to the API server's application."""
    for method, path, handler in (
        ("GET", "/capabilities", capabilities),
        ("GET", "/notices", notices),
        ("POST", "/devices", devices),
        ("POST", "/upload", upload_file),
        ("GET", "/search", search_messages),
        ("GET", "/todos", get_todos),
    ):
        app.router.add_route(method, PREFIX + path, _authed(adapter, handler))


def register(ctx) -> None:
    ctx.register_platform_handler("api_server", wire)
