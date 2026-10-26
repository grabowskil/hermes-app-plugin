import asyncio
import os
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


def test_read_port(tmp_path):
    assert host_browser._read_port(str(tmp_path)) is None
    f = tmp_path / "DevToolsActivePort"
    f.write_text("41234\n")
    assert host_browser._read_port(str(tmp_path)) is None  # ws path not written yet
    f.write_text("41234\n/devtools/browser/abc-123\n")
    assert host_browser._read_port(str(tmp_path)) == (41234, "/devtools/browser/abc-123")


def test_close_removes_profile(tmp_path, monkeypatch):
    profile = tmp_path / "hermes-site-x"
    profile.mkdir()
    (profile / "Cookies").write_text("x")
    monkeypatch.setattr(host_browser, "_pids", lambda p: [])

    async def run():
        timer = asyncio.get_running_loop().call_later(900, lambda: None)
        host_browser._sessions["s"] = {"profile": str(profile), "port": 1, "ws": "/x", "timer": timer}
        await host_browser.close("s")
        await host_browser.close("s")  # second close is a no-op

    asyncio.run(run())
    assert not profile.exists()
    assert "s" not in host_browser._sessions


def test_start_rejects_bad_url():
    with pytest.raises(ValueError):
        asyncio.run(host_browser.start("s", "javascript:alert(1)"))
