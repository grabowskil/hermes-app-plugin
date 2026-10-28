import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_app import routes, voice

KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}


class FakeAdapter:
    _api_key = KEY


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(voice, "_cache", None)
    clock = {"now": 1000.0}
    monkeypatch.setattr(voice, "_now", lambda: clock["now"])
    return clock


def _stub(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _run(coro_fn):
    async def main():
        app = web.Application()
        routes.wire(app, FakeAdapter())
        async with TestClient(TestServer(app)) as client:
            return await coro_fn(client)

    return asyncio.run(main())


def _stt_stub(monkeypatch, result):
    seen = []

    def transcribe_recording(path):
        seen.append(Path(path))
        assert Path(path).read_bytes() == b"AUDIO"
        return result

    _stub(monkeypatch, "tools.voice_mode", transcribe_recording=transcribe_recording)
    return seen


def _post_audio(content_type, body=b"AUDIO"):
    async def go(c):
        r = await c.post("/app/v1/voice/transcribe", data=body, headers={**AUTH, "Content-Type": content_type})
        return r.status, await r.json()

    return go


def test_transcribe_returns_transcript_and_deletes_the_file(monkeypatch):
    seen = _stt_stub(monkeypatch, {"success": True, "transcript": " hello there "})
    assert _run(_post_audio("audio/mp4")) == (200, {"transcript": "hello there", "provider": None})
    assert seen[0].suffix == ".m4a"
    assert not seen[0].exists()


def test_silence_is_an_empty_transcript(monkeypatch):
    _stt_stub(monkeypatch, {"success": True, "transcript": "", "no_speech": True})
    assert _run(_post_audio("audio/wav")) == (200, {"transcript": "", "provider": None})


def test_empty_body_and_unknown_type_are_400(monkeypatch):
    _stt_stub(monkeypatch, {"success": True, "transcript": "x"})
    assert _run(_post_audio("audio/mp4", body=b""))[0] == 400
    assert _run(_post_audio("image/png"))[0] == 400


def test_provider_error_is_502(monkeypatch):
    _stt_stub(monkeypatch, {"success": False, "error": "no backend"})
    assert _run(_post_audio("audio/mpeg")) == (502, {"error": "transcription failed"})


def test_speak_returns_audio_and_removes_the_file(monkeypatch, tmp_path):
    out = tmp_path / "reply.mp3"
    out.write_bytes(b"MP3DATA")
    _stub(monkeypatch, "tools.tts_tool", text_to_speech_tool=lambda text: json.dumps({"success": True, "file_path": str(out)}))

    async def go(c):
        r = await c.post("/app/v1/voice/speak", json={"text": "  hi  "}, headers=AUTH)
        return r.status, r.headers["Content-Type"], await r.read()

    assert _run(go) == (200, "audio/mpeg", b"MP3DATA")
    assert not out.exists()


def test_speak_rejects_empty_text_and_reports_failures(monkeypatch):
    _stub(monkeypatch, "tools.tts_tool", text_to_speech_tool=lambda text: json.dumps({"success": False, "error": "x"}))

    async def empty(c):
        return (await c.post("/app/v1/voice/speak", json={"text": "   "}, headers=AUTH)).status

    async def failing(c):
        r = await c.post("/app/v1/voice/speak", json={"text": "hi"}, headers=AUTH)
        return r.status, await r.json()

    assert _run(empty) == 400
    assert _run(failing) == (502, {"error": "speech failed"})


def test_features_follow_config_and_are_cached_for_60_seconds(monkeypatch, _isolate):
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("stt:\n  enabled: true\n")
    calls = []

    def check_tts_requirements():
        calls.append(1)
        return True

    _stub(monkeypatch, "tools.voice_mode")
    _stub(monkeypatch, "tools.tts_tool", check_tts_requirements=check_tts_requirements)

    assert voice.features() == ["voice_stt", "voice_tts"]
    assert voice.features() == ["voice_stt", "voice_tts"]
    assert len(calls) == 1
    _isolate["now"] += 61
    assert voice.features() == ["voice_stt", "voice_tts"]
    assert len(calls) == 2


def test_features_stay_off_without_stt_enabled_or_tools(monkeypatch):
    _stub(monkeypatch, "tools.tts_tool", check_tts_requirements=lambda: False)
    assert voice.features() == []
