"""Voice mode for ``/app/v1/voice/*``: speech-to-text and text-to-speech through Hermes's own tools.

The providers (``stt.*``, ``tts.*``) are configured in the Hermes config, not here. Both entry
points block, so the routes run them in an executor. ``tools.*`` is imported inside the functions,
so the plugin loads on a Hermes without them; the features then simply stay off.
"""
import importlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

MAX_BYTES = 10_000_000  # the API server rejects larger bodies itself
MAX_TEXT = 4000
CACHE_SECONDS = 60
EXTENSIONS = {
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/x-caf": ".caf",
}
SPEECH_TYPES = {".mp3": "audio/mpeg", ".ogg": "audio/ogg"}

_now = time.monotonic
_cache: Optional[tuple] = None  # (checked at, features)


class VoiceError(Exception):
    """The Hermes tool failed; the message is safe to return to the app."""


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def _stt_enabled() -> bool:
    """``stt.enabled: true`` in the Hermes config. Missing config or key means off."""
    try:
        import yaml

        raw = yaml.safe_load((_hermes_home() / "config.yaml").read_text()) or {}
    except Exception:
        return False
    return bool((raw.get("stt") or {}).get("enabled"))


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
    except Exception:
        return False
    return True


def _tts_ready() -> bool:
    try:
        from tools.tts_tool import check_tts_requirements

        return bool(check_tts_requirements())
    except Exception:
        return False


def features() -> list:
    """``voice_stt`` / ``voice_tts`` when the Hermes side can serve them; cached for 60 s.

    Blocking (the TTS check may import edge-tts): call it from an executor.
    """
    global _cache
    now = _now()
    if _cache is not None and now - _cache[0] < CACHE_SECONDS:
        return list(_cache[1])
    found = []
    if _stt_enabled() and _importable("tools.voice_mode"):
        found.append("voice_stt")
    if _tts_ready():
        found.append("voice_tts")
    _cache = (now, found)
    return list(found)


def transcribe(body: bytes, suffix: str) -> dict:
    """Write the audio to a temp file, run Hermes's STT chain on it, delete the file. Blocking."""
    from tools.voice_mode import transcribe_recording

    fd, path = tempfile.mkstemp(suffix=suffix, prefix="hermes_app_voice_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        return transcribe_recording(path)
    finally:
        Path(path).unlink(missing_ok=True)


def speak(text: str) -> tuple:
    """Synthesize ``text`` with Hermes's TTS and return ``(audio bytes, content type)``. Blocking."""
    from tools.tts_tool import text_to_speech_tool

    result = json.loads(text_to_speech_tool(text))
    if not result.get("success"):
        raise VoiceError(str(result.get("error") or "speech failed"))
    path = Path(result["file_path"])
    try:
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    return data, SPEECH_TYPES.get(path.suffix.lower(), "audio/mpeg")
