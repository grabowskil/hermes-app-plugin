"""A throwaway browser on the host for logins that need a real browser (passkeys, Google OAuth).

``start`` launches Chrome/Chromium with a fresh temporary profile and no debugging interface (Google
refuses sign-in in a browser that has one). The user signs in at the host's screen. For cookie
logins ``cookies`` quits the browser cleanly (SIGTERM flushes the cookie store), reads the profile's
``Cookies`` SQLite file and decrypts the values; ``close`` kills leftovers by profile path and deletes
the profile. One session per site, closed automatically after 15 minutes.

The profile uses ``--use-mock-keychain``, so values are encrypted with a fixed, well-known key
(``v10``, AES-128-CBC) and no Keychain prompt appears. On macOS the browser is started through
``open -n -a`` (from a launchd background session only ``open`` lands in the GUI session); elsewhere by
direct exec. Cookie values and URLs are never logged.
"""
import asyncio
import hashlib
import logging
import os
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

TTL = 900
START_TIMEOUT = 20
QUIT_TIMEOUT = 15
KILL_TIMEOUT = 5
MAC_APPS = ("/Applications/Google Chrome.app", "/Applications/Chromium.app")
LINUX_BINARIES = ("google-chrome", "chromium", "chromium-browser")
_EPOCH_DELTA_S = 11644473600  # 1601-01-01 to 1970-01-01

# site_id -> {"profile": str, "pid": int, "timer": TimerHandle}
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


def _main_pid(profile: str):
    """The browser's main process for ``profile`` (the one without ``--type=``), else None."""
    pids = _pids(profile)
    if not pids:
        return None
    try:
        out = subprocess.run(["ps", "-ww", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and " --type=" not in command and f"--user-data-dir={profile}" in command:
            return int(pid)
    return None


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
    args = [f"--user-data-dir={profile}", "--use-mock-keychain", "--no-first-run",
            "--no-default-browser-check", url]
    kind, path = target
    try:
        pid = None
        if sys.platform == "darwin" and kind == "app":
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-n", "-a", path, "--args", *args,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await proc.wait()  # returns before the browser runs: find its main process
        else:
            if kind == "app":
                raise RuntimeError("no browser")
            pid = subprocess.Popen([path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True).pid
        end = time.monotonic() + START_TIMEOUT
        while pid is None and time.monotonic() < end:
            pid = _main_pid(profile)
            if pid is None:
                await asyncio.sleep(0.25)
        if pid is None:
            raise RuntimeError("browser did not start")
    except BaseException:
        await _kill_and_remove(profile)
        raise
    loop = asyncio.get_running_loop()
    timer = loop.call_later(TTL, lambda: loop.create_task(close(site_id)))
    _sessions[site_id] = {"profile": profile, "pid": pid, "timer": timer}
    log.info("host browser started for site %s", site_id)


def _session(site_id: str) -> dict:
    s = _sessions.get(site_id)
    if s is None:
        raise KeyError(site_id)
    return s


def _derive_key() -> bytes:
    return hashlib.pbkdf2_hmac("sha1", b"mock_password", b"saltysalt", 1003, 16)


def _decrypt(blob: bytes, host_key: str, strip_hash: bool):
    """The plain value of a ``v10`` blob, or None when it is not one or does not decrypt."""
    if not blob.startswith(b"v10"):
        return None
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise RuntimeError("the 'cryptography' package is missing in the gateway environment") from None
    try:
        dec = Cipher(algorithms.AES(_derive_key()), modes.CBC(b" " * 16)).decryptor()
        padded = dec.update(blob[3:]) + dec.finalize()
        unpad = padding.PKCS7(128).unpadder()
        plain = unpad.update(padded) + unpad.finalize()
    except ValueError:
        return None
    if strip_hash:
        if plain[:32] != hashlib.sha256(host_key.encode("utf-8")).digest():
            return None
        plain = plain[32:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_cookies(profile: str) -> list:
    db = Path(profile) / "Default" / "Cookies"
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            strip_hash = int(row[0]) >= 24 if row else False
        except (sqlite3.Error, ValueError, TypeError):
            strip_hash = False
        rows = con.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, "
            "has_expires, is_persistent FROM cookies").fetchall()
    finally:
        con.close()
    out = []
    for host, name, value, blob, path, expires_utc, secure, httponly, has_expires, persistent in rows:
        if blob:
            value = _decrypt(bytes(blob), host, strip_hash)
            if value is None:
                continue
        expires = None
        if has_expires and persistent and expires_utc:
            expires = expires_utc / 1e6 - _EPOCH_DELTA_S
        out.append({"name": name, "value": value or "", "domain": host, "path": path or "/",
                    "expires": expires, "secure": bool(secure), "http_only": bool(httponly)})
    return out


async def _quit(s: dict) -> None:
    """Ask the browser to quit (SIGTERM flushes cookies); SIGKILL whatever is left after the timeout."""
    _signal([s["pid"]], signal.SIGTERM)
    if await _wait_gone(s["profile"], QUIT_TIMEOUT):
        _signal(_pids(s["profile"]), signal.SIGKILL)
        await _wait_gone(s["profile"], 2)


async def cookies(site_id: str) -> list:
    """Stop the browser (flushing its cookie store), then all its cookies in the app's cookie format."""
    s = _session(site_id)
    await _quit(s)
    return await asyncio.to_thread(_read_cookies, s["profile"])


def _chrome_major(kind: str, path: str) -> str:
    if kind == "app":
        with open(Path(path) / "Contents" / "Info.plist", "rb") as f:
            version = plistlib.load(f).get("CFBundleShortVersionString", "")
    else:
        version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    m = re.search(r"(\d+)\.", version)
    if not m:
        raise RuntimeError("browser version unknown")
    return m.group(1)


async def user_agent(site_id: str) -> str:
    """Chrome's reduced user agent for the installed browser version."""
    _session(site_id)
    target = _resolve()
    if target is None:
        raise RuntimeError("no browser")
    try:
        major = await asyncio.to_thread(_chrome_major, *target)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError("browser version unknown") from None
    platform = "Macintosh; Intel Mac OS X 10_15_7" if sys.platform == "darwin" else "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


async def close(site_id: str) -> None:
    """Quit the browser, kill leftovers by profile path, delete the profile. Never raises."""
    s = _sessions.pop(site_id, None)
    if s is None:
        return
    s["timer"].cancel()
    await _kill_and_remove(s["profile"])


async def close_all() -> None:
    for site_id in list(_sessions):
        await close(site_id)
