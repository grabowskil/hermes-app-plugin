import asyncio
import hashlib
import plistlib
import signal
import sqlite3
import sys

import pytest

from hermes_app import host_browser


def test_available_with_override(tmp_path, monkeypatch):
    exe = tmp_path / "fakechrome"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("HERMES_APP_BROWSER", str(exe))
    assert host_browser.available()
    assert host_browser._resolve() == ("bin", str(exe))


def test_not_available_with_bad_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_APP_BROWSER", str(tmp_path / "missing"))
    assert not host_browser.available()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS app bundles")
def test_override_app_bundle(tmp_path, monkeypatch):
    app = tmp_path / "Fake.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setenv("HERMES_APP_BROWSER", str(app))
    assert host_browser._resolve() == ("app", str(app))


def _encrypt(plain: bytes, host_key: str, with_hash: bool) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = hashlib.pbkdf2_hmac("sha1", b"mock_password", b"saltysalt", 1003, 16)
    if with_hash:
        plain = hashlib.sha256(host_key.encode()).digest() + plain
    pad = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(pad.update(plain) + pad.finalize()) + enc.finalize()


def _make_cookies_db(profile, version, with_hash):
    d = profile / "Default"
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "Cookies")
    con.execute("CREATE TABLE meta (key TEXT UNIQUE NOT NULL, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT, "
                "expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, has_expires INTEGER, "
                "is_persistent INTEGER)")
    exp = int((1_800_000_000 + 11644473600) * 1e6)
    rows = [
        (".example.com", "SID", "", _encrypt(b"secret-1", ".example.com", with_hash), "/", exp, 1, 1, 1, 1),
        (".example.com", "sess", "", _encrypt("wert-\u00e4".encode(), ".example.com", with_hash), "/a", 0, 0, 0, 0, 0),
        ("example.com", "plain", "visible", b"", "/", exp, 0, 0, 1, 0),  # has_expires but not persistent
        ("example.com", "junk", "", b"v10garbage", "/", 0, 0, 0, 0, 0),  # undecryptable: skipped
    ]
    con.executemany("INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


@pytest.mark.parametrize("version,with_hash", [(24, True), (23, False)])
def test_read_cookies_decrypts_and_maps(tmp_path, version, with_hash):
    _make_cookies_db(tmp_path, version, with_hash)
    got = {c["name"]: c for c in host_browser._read_cookies(str(tmp_path))}
    assert set(got) == {"SID", "sess", "plain"}
    assert got["SID"] == {"name": "SID", "value": "secret-1", "domain": ".example.com", "path": "/",
                          "expires": pytest.approx(1_800_000_000), "secure": True, "http_only": True}
    assert got["sess"]["value"] == "wert-\u00e4" and got["sess"]["expires"] is None
    assert got["sess"]["path"] == "/a" and got["sess"]["secure"] is False
    assert got["plain"]["value"] == "visible" and got["plain"]["expires"] is None


def test_read_cookies_without_db(tmp_path):
    assert host_browser._read_cookies(str(tmp_path)) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS app bundles")
def test_user_agent_from_plist(tmp_path, monkeypatch):
    app = tmp_path / "Fake.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": "142.0.7444.60"}, f)
    monkeypatch.setenv("HERMES_APP_BROWSER", str(app))

    async def run():
        timer = asyncio.get_running_loop().call_later(900, lambda: None)
        host_browser._sessions["s"] = {"profile": "/nowhere", "pid": 1, "timer": timer}
        try:
            return await host_browser.user_agent("s")
        finally:
            timer.cancel()
            host_browser._sessions.pop("s")

    assert asyncio.run(run()) == ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")


def test_cookies_quits_browser_before_reading(tmp_path, monkeypatch):
    _make_cookies_db(tmp_path, 24, True)
    sent = []
    monkeypatch.setattr(host_browser, "_signal", lambda pids, sig: sent.append((list(pids), sig)))
    monkeypatch.setattr(host_browser, "_pids", lambda p: [])

    async def run():
        timer = asyncio.get_running_loop().call_later(900, lambda: None)
        host_browser._sessions["s"] = {"profile": str(tmp_path), "pid": 4242, "timer": timer}
        try:
            return await host_browser.cookies("s")
        finally:
            timer.cancel()
            host_browser._sessions.pop("s")

    assert len(asyncio.run(run())) == 3
    assert sent == [([4242], signal.SIGTERM)]


def test_close_removes_profile(tmp_path, monkeypatch):
    profile = tmp_path / "hermes-site-x"
    profile.mkdir()
    (profile / "Cookies").write_text("x")
    monkeypatch.setattr(host_browser, "_pids", lambda p: [])

    async def run():
        timer = asyncio.get_running_loop().call_later(900, lambda: None)
        host_browser._sessions["s"] = {"profile": str(profile), "pid": 1, "timer": timer}
        await host_browser.close("s")
        await host_browser.close("s")  # second close is a no-op

    asyncio.run(run())
    assert not profile.exists()
    assert "s" not in host_browser._sessions


def test_start_rejects_bad_url():
    with pytest.raises(ValueError):
        asyncio.run(host_browser.start("s", "javascript:alert(1)"))
