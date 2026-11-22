"""``/app/v1/agents`` routes: third-party agent sessions (listing, following, continuing, starting)."""
import asyncio
import json

from aiohttp import web

from . import get as get_provider, describe_all, runs

KEEPALIVE_SECONDS = 15


def _unknown() -> web.Response:
    return web.json_response({"error": "unknown provider"}, status=404)


def _not_found() -> web.Response:
    return web.json_response({"error": "not_found"}, status=404)


def _int(request: web.Request, name: str, default: int) -> int:
    try:
        return int(request.query.get(name, default))
    except ValueError:
        return default


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _public(session: dict) -> dict:
    """Session as sent to the app: no holder pid; our own runs read as busy, not live elsewhere."""
    session = dict(session)
    holder = session.pop("holder_pid", None)
    if holder is not None and holder in runs.own_pids():
        session["live"], session["busy"] = False, True
    return session


def wire(app: web.Application, authed, prefix: str) -> None:
    base = prefix + "/agents"

    def provider_of(request):
        return get_provider(request.match_info["p"])

    async def providers(request):
        data = await asyncio.to_thread(describe_all)
        if isinstance(data, dict) and "providers" in data:
            data = data["providers"]
        return web.json_response({"providers": data})

    async def environments(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        return web.json_response({"environments": await asyncio.to_thread(prov.environments)})

    async def sessions(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        data = await asyncio.to_thread(prov.sessions, _int(request, "limit", 100))
        return web.json_response({"data": [_public(s) for s in data]})

    async def session_get(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        session = await asyncio.to_thread(prov.session, request.match_info["id"])
        if not session:
            return _not_found()
        return web.json_response({"session": _public(session)})

    async def _session_out(prov, result):
        """Providers may hand back a Session, or just its id."""
        if isinstance(result, dict) and "title" in result:
            return result
        sid = result.get("id") if isinstance(result, dict) else result
        return await asyncio.to_thread(prov.session, sid)

    async def session_create(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        body = await _json(request)
        try:
            result = await asyncio.to_thread(prov.create_draft, body.get("environment_id"), body.get("options") or {})
        except KeyError:
            return web.json_response({"error": "unknown environment"}, status=404)
        session = await _session_out(prov, result)
        return web.json_response({"session": _public(session or {})})

    async def session_patch(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        sid = request.match_info["id"]
        body = await _json(request)
        try:
            await asyncio.to_thread(prov.set_options, sid, body.get("options") or {})
        except KeyError:
            return _not_found()
        session = await asyncio.to_thread(prov.session, sid)
        if not session:
            return _not_found()
        return web.json_response({"session": _public(session)})

    async def session_fork(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        try:
            result = await asyncio.to_thread(prov.fork_draft, request.match_info["id"])
        except KeyError:
            return _not_found()
        session = await _session_out(prov, result)
        return web.json_response({"session": _public(session or {})})

    async def messages(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        sid = request.match_info["id"]
        limit = _int(request, "limit", 100)
        try:
            if "after_id" in request.query:
                out = await asyncio.to_thread(prov.messages_after, sid, _int(request, "after_id", 0), limit)
            else:
                data = await asyncio.to_thread(prov.messages, sid, limit, _int(request, "offset", 0))
                out = {"data": data}
        except KeyError:
            return _not_found()
        return web.json_response(out)

    async def activity(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        sid = request.match_info["id"]
        try:
            out = dict(await asyncio.to_thread(prov.activity, sid))
        except KeyError:
            return _not_found()
        run_id = runs.active_run(sid)
        if run_id:
            out.update({"active": True, "kind": "run", "run_id": run_id})
        return web.json_response(out)

    async def run_create(request):
        prov = provider_of(request)
        if prov is None:
            return _unknown()
        text = (await _json(request)).get("input")
        if not isinstance(text, str) or not text.strip():
            return web.json_response({"error": "input required"}, status=400)
        try:
            return web.json_response(await runs.start(prov, request.match_info["id"], text))
        except runs.LiveElsewhere as exc:
            return web.json_response({"error": "live_elsewhere", "open_url": exc.open_url}, status=409)
        except runs.Busy:
            return web.json_response({"error": "busy"}, status=409)
        except KeyError:
            return _not_found()

    def run_of(request):
        run = runs.get(request.match_info["run"])
        if run is None or run.provider != request.match_info["p"]:
            return None
        return run

    async def run_events(request):
        if get_provider(request.match_info["p"]) is None:
            return _unknown()
        run = run_of(request)
        if run is None:
            return _not_found()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                               "X-Accel-Buffering": "no"})
        await response.prepare(request)
        gen = runs.events(run.id)
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(gen.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=KEEPALIVE_SECONDS)
                if not done:
                    await response.write(b": keepalive\n\n")
                    continue
                task, pending = pending, None
                try:
                    event = task.result()
                except StopAsyncIteration:
                    break
                await response.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if pending is not None:
                pending.cancel()
            await gen.aclose()
        try:
            await response.write(b": stream closed\n\n")
            await response.write_eof()
        except Exception:
            pass
        return response

    async def run_status(request):
        if get_provider(request.match_info["p"]) is None:
            return _unknown()
        run = run_of(request)
        if run is None:
            return _not_found()
        return web.json_response(runs.status(run.id))

    async def run_stop(request):
        if get_provider(request.match_info["p"]) is None:
            return _unknown()
        run = run_of(request)
        if run is None:
            return _not_found()
        await runs.stop(run.id)
        return web.json_response({"status": "stopping"})

    for method, path, handler in (
        ("GET", "", providers),
        ("GET", "/{p}/environments", environments),
        ("GET", "/{p}/sessions", sessions),
        ("POST", "/{p}/sessions", session_create),
        ("GET", "/{p}/sessions/{id}", session_get),
        ("PATCH", "/{p}/sessions/{id}", session_patch),
        ("POST", "/{p}/sessions/{id}/fork", session_fork),
        ("GET", "/{p}/sessions/{id}/messages", messages),
        ("GET", "/{p}/sessions/{id}/activity", activity),
        ("POST", "/{p}/sessions/{id}/runs", run_create),
        ("GET", "/{p}/runs/{run}/events", run_events),
        ("GET", "/{p}/runs/{run}", run_status),
        ("POST", "/{p}/runs/{run}/stop", run_stop),
    ):
        app.router.add_route(method, base + path, authed(handler))
