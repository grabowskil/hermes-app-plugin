"""A throwaway browser on the host for logins that need a real browser (passkeys, Google OAuth).

``start`` launches Chrome/Chromium with a fresh temporary profile and a random loopback debugging
port; the user signs in at the host's screen; the caller then reads the cookies (``cookies``) or the
open tab URLs (``page_urls``) and calls ``close``, which quits the browser, kills leftovers by
profile path and deletes the profile. One session per site, closed automatically after 15 minutes.

On macOS the browser is started through ``open -n -a`` (from a launchd background session only
``open`` lands in the GUI session with keychain access); elsewhere by direct exec. Cookie values
and URLs are never logged.
"""
import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

TTL = 900
START_TIMEOUT = 20
KILL_TIMEOUT = 5
MAC_APPS = ("/Applications/Google Chrome.app", "/Applications/Chromium.app")
LINUX_BINARIES = ("google-chrome", "chromium", "chromium-browser")
_HTTP = aiohttp.ClientTimeout(total=10)

# site_id -> {"profile": str, "port": int, "ws": str, "timer": TimerHandle}
_sessions: dict = {}


def _resolve():
    """The browser to launch as ``("app", <.app path>)`` or ``("bin", <binary path>)``, else None."""
    override = os.environ.get("HERMES_APP_BROWSER")
    if override:
        p = Path(override)
        if sys.platform == "darwin":
            for parent in (p, *p.parents):
                if parent.suffix == ".app" and parent.is_dir():
                    return ("app", str(parent))
        if p.is_file() and os.access(p, os.X_OK):
            return ("bin", str(p))
        return None
    if sys.platform == "darwin":
        for app in MAC_APPS:
            if Path(app).is_dir():
                return ("app", app)
        return None
    for name in LINUX_BINARIES:
        found = shutil.which(name)
        if found:
            return ("bin", found)
    return None


def available() -> bool:
    return _resolve() is not None


def _read_port(profile: str):
    """``(port, ws_path)`` from ``<profile>/DevToolsActivePort`` once both lines are there, else None."""
    try:
        lines = (Path(profile) / "DevToolsActivePort").read_text().splitlines()
        if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].strip().startswith("/"):
            return int(lines[0].strip()), lines[1].strip()
    except OSError:
        pass
    return None


def _pids(profile: str) -> list:
    """Process ids whose command line contains the profile path."""
    try:
        out = subprocess.run(["pgrep", "-f", re.escape(profile)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()]


def _signal(pids, sig) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


async def _wait_gone(profile: str, timeout: float) -> list:
    end = time.monotonic() + timeout
    pids = _pids(profile)
    while pids and time.monotonic() < end:
        await asyncio.sleep(0.25)
        pids = _pids(profile)
    return pids


async def _kill_and_remove(profile: str) -> None:
    pids = _pids(profile)
    if pids:
        _signal(pids, signal.SIGTERM)
        pids = await _wait_gone(profile, KILL_TIMEOUT)
        if pids:
            _signal(pids, signal.SIGKILL)
            await _wait_gone(profile, 2)
    shutil.rmtree(profile, ignore_errors=True)


async def start(site_id: str, url: str) -> None:
    """Open ``url`` in a fresh browser. RuntimeError when no browser is found or it does not come up."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("bad url")
    target = _resolve()
    if target is None:
        raise RuntimeError("no browser")
    await close(site_id)
    profile = tempfile.mkdtemp(prefix="hermes-site-")
    os.chmod(profile, 0o700)
    args = [f"--user-data-dir={profile}", "--remote-debugging-port=0", "--no-first-run",
            "--no-default-browser-check", "--new-window", "--", url]
    kind, path = target
    try:
        if sys.platform == "darwin" and kind == "app":
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-n", "-a", path, "--args", *args,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await proc.wait()
        else:
            if kind == "app":
                raise RuntimeError("no browser")
            subprocess.Popen([path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        found, end = None, time.monotonic() + START_TIMEOUT
        while found is None and time.monotonic() < end:
            found = _read_port(profile)
            if found is None:
                await asyncio.sleep(0.25)
        if found is None:
            raise RuntimeError("browser did not start")
    except BaseException:
        await _kill_and_remove(profile)
        raise
    loop = asyncio.get_running_loop()
    timer = loop.call_later(TTL, lambda: loop.create_task(close(site_id)))
    _sessions[site_id] = {"profile": profile, "port": found[0], "ws": found[1], "timer": timer}
    log.info("host browser started for site %s", site_id)


def _session(site_id: str) -> dict:
    s = _sessions.get(site_id)
    if s is None:
        raise KeyError(site_id)
    return s


async def _get_json(site_id: str, path: str):
    s = _session(site_id)
    async with aiohttp.ClientSession(timeout=_HTTP) as http:
        async with http.get(f"http://127.0.0.1:{s['port']}{path}") as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def _command(s: dict, method: str):
    async with aiohttp.ClientSession(timeout=_HTTP) as http:
        async with http.ws_connect(f"ws://127.0.0.1:{s['port']}{s['ws']}") as ws:
            await ws.send_json({"id": 1, "method": method})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = msg.json()
                if data.get("id") == 1:
                    if "error" in data:
                        raise RuntimeError("browser command failed")
                    return data.get("result") or {}
    return {}


async def cookies(site_id: str) -> list:
    """All cookies of the browser in the app's cookie format."""
    result = await _command(_session(site_id), "Storage.getCookies")
    out = []
    for c in result.get("cookies", []):
        exp = c.get("expires")
        out.append({
            "name": c.get("name"), "value": c.get("value"), "domain": c.get("domain"),
            "path": c.get("path") or "/",
            "expires": None if c.get("session") or not isinstance(exp, (int, float)) or exp <= 0 else exp,
            "secure": bool(c.get("secure")), "http_only": bool(c.get("httpOnly")),
        })
    return out


async def page_urls(site_id: str) -> list:
    """URLs of the open tabs."""
    return [t["url"] for t in await _get_json(site_id, "/json/list")
            if t.get("type") == "page" and isinstance(t.get("url"), str)]


async def user_agent(site_id: str) -> str:
    return (await _get_json(site_id, "/json/version")).get("User-Agent", "")


async def close(site_id: str) -> None:
    """Quit the browser, kill leftovers by profile path, delete the profile. Never raises."""
    s = _sessions.pop(site_id, None)
    if s is None:
        return
    s["timer"].cancel()
    try:
        await _command(s, "Browser.close")
    except Exception:
        pass
    await _wait_gone(s["profile"], 2)
    await _kill_and_remove(s["profile"])


async def close_all() -> None:
    for site_id in list(_sessions):
        await close(site_id)
