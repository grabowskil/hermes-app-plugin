"""Provider-agnostic run engine: spawn an agent CLI, translate its output to run events, buffer and follow them."""
import asyncio
import json
import os
import signal
import time
import uuid
from typing import AsyncIterator, Optional

TERMINAL = ("run.completed", "run.failed", "run.cancelled")
PRUNE_AFTER = 30 * 60
STOP_GRACE = 5.0


class LiveElsewhere(Exception):
    def __init__(self, open_url=None):
        super().__init__("live_elsewhere")
        self.open_url = open_url


class Busy(Exception):
    pass


class Run:
    def __init__(self, provider: str, session_id: str):
        self.id = "arun_" + uuid.uuid4().hex[:12]
        self.provider = provider
        self.session_id = session_id
        self.proc = None
        self.events: list = []
        self.status = "running"
        self.output = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.stopped = False
        self.cond = asyncio.Condition()
        self.task = None

    def terminal(self) -> bool:
        return self.status != "running"


_runs: dict = {}


def _prune() -> None:
    now = time.time()
    for rid in [r for r, run in _runs.items() if run.finished_at and now - run.finished_at > PRUNE_AFTER]:
        _runs.pop(rid, None)


def own_pids() -> set:
    return {r.proc.pid for r in _runs.values() if r.proc is not None and not r.terminal()}


def active_run(sid: str) -> Optional[str]:
    for rid, run in _runs.items():
        if run.session_id == sid and not run.terminal():
            return rid
    return None


def get(run_id: str) -> Optional[Run]:
    return _runs.get(run_id)


def status(run_id: str) -> Optional[dict]:
    run = _runs.get(run_id)
    if run is None:
        return None
    return {"status": run.status, "output": run.output}


async def _emit(run: Run, event: dict) -> None:
    event = {**event, "run_id": run.id, "timestamp": round(time.time(), 2)}
    async with run.cond:
        run.events.append(event)
        name = event["event"]
        if name == "run.completed":
            run.status, run.output = "completed", event.get("output") or ""
        elif name == "run.failed":
            run.status = "failed"
        elif name == "run.cancelled":
            run.status = "cancelled"
        if run.terminal():
            run.finished_at = time.time()
        run.cond.notify_all()


async def _drain_stderr(stream, sink: list) -> None:
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            sink.append(chunk)
            del sink[:-8]
    except Exception:
        return


async def _pump(run: Run, provider, sid: str, was_draft: bool) -> None:
    proc = run.proc
    state: dict = {}
    err: list = []
    err_task = asyncio.ensure_future(_drain_stderr(proc.stderr, err))
    settled = False

    async def settle():
        """Refresh the provider's cache (and retire a draft) before followers see the end."""
        nonlocal settled
        if settled:
            return
        settled = True
        try:
            provider.invalidate(sid)
            if was_draft:
                located = await asyncio.to_thread(provider.locate, sid)
                if located and located.get("path") and os.path.exists(located["path"]):
                    provider.drop_draft(sid)
        except Exception:
            pass

    try:
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # an oversized line: skip it
            if not line:
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            for event in provider.translate(obj, state):
                if run.stopped and event["event"] in TERMINAL:
                    event = {"event": "run.cancelled"}
                if event["event"] in TERMINAL:
                    await settle()
                if not run.terminal():
                    await _emit(run, event)
        await proc.wait()
        await err_task
        if not run.terminal():
            await settle()
            if run.stopped:
                await _emit(run, {"event": "run.cancelled"})
            else:
                tail = b"".join(err).decode("utf-8", "replace").strip()[-500:]
                await _emit(run, {"event": "run.failed", "error": tail or "agent exited (%s)" % proc.returncode})
    except Exception as exc:  # never leave followers hanging
        if not run.terminal():
            await _emit(run, {"event": "run.failed", "error": str(exc)})
    finally:
        await settle()


async def start(provider, sid: str, text: str) -> dict:
    _prune()
    if active_run(sid):
        raise Busy()
    located = await asyncio.to_thread(provider.locate, sid)
    if not located:
        raise KeyError(sid)
    holder = located.get("holder_pid")
    if located.get("live") and holder not in own_pids():
        raise LiveElsewhere(located.get("open_url"))
    options = await asyncio.to_thread(provider.get_options, sid)
    argv, cwd, stdin_text = provider.command(provider.binary(), sid, text, options, located)
    env_fn = getattr(provider, "child_env", None)
    if env_fn is None:
        from .claude_code_run import child_env as env_fn
    run = Run(provider.ID, sid)
    run.proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd, env=env_fn(), start_new_session=True, limit=32 * 1024 * 1024)
    _runs[run.id] = run
    try:
        run.proc.stdin.write(stdin_text.encode("utf-8"))
        await run.proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            run.proc.stdin.close()
        except Exception:
            pass
    run.task = asyncio.ensure_future(_pump(run, provider, sid, bool(located.get("draft"))))
    return {"run_id": run.id, "session_id": sid}


async def events(run_id: str) -> AsyncIterator[dict]:
    """Replay from the first event, then follow until a terminal event."""
    run = _runs.get(run_id)
    if run is None:
        return
    index = 0
    while True:
        async with run.cond:
            while index >= len(run.events) and not run.terminal():
                await run.cond.wait()
            batch = run.events[index:]
            index = len(run.events)
            done = run.terminal() and index >= len(run.events)
        for event in batch:
            yield event
            if event["event"] in TERMINAL:
                return
        if done and not batch:
            return


def _signal_group(proc, sig) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


async def stop(run_id: str) -> bool:
    run = _runs.get(run_id)
    if run is None:
        return False
    if run.terminal() or run.proc is None:
        return True
    run.stopped = True
    _signal_group(run.proc, signal.SIGINT)

    async def _kill():
        try:
            await asyncio.wait_for(run.proc.wait(), STOP_GRACE)
        except asyncio.TimeoutError:
            _signal_group(run.proc, signal.SIGKILL)

    asyncio.ensure_future(_kill())
    return True
