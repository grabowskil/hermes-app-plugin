"""``GET /app/v1/model/options``: provider checks and the session's locked model."""
import json
import sqlite3
import sys
import types

import pytest

from hermes_app import model_options

PROVIDERS = [
    {"slug": "nous", "name": "Nous Portal", "authenticated": True, "models": ["hermes-4"]},
    {"slug": "openrouter", "name": "OpenRouter", "authenticated": True, "models": ["anthropic/claude-sonnet-5"]},
    {"slug": "custom", "name": "Custom", "authenticated": True, "models": []},
]


@pytest.fixture
def resolver_calls(monkeypatch):
    calls = []

    def build_model_options_payload(ctx, *, include_unconfigured, refresh):
        return {"model": "anthropic/claude-sonnet-5", "provider": "openrouter",
                "providers": [dict(row) for row in PROVIDERS]}

    def resolve_runtime_provider(*, requested=None):
        calls.append(requested)
        if requested == "nous":
            raise RuntimeError("Hermes is not logged into Nous Portal.")
        return {"provider": requested}

    inventory = types.ModuleType("hermes_cli.inventory")
    inventory.build_model_options_payload = build_model_options_payload
    inventory.load_picker_context = lambda: "ctx"
    runtime = types.ModuleType("hermes_cli.runtime_provider")
    runtime.resolve_runtime_provider = resolve_runtime_provider
    package = types.ModuleType("hermes_cli")
    package.inventory = inventory
    package.runtime_provider = runtime
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.inventory", inventory)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime)
    return calls


def test_provider_that_fails_the_resolver_is_marked_unusable(resolver_calls):
    rows = {row["slug"]: row for row in model_options.build(None)["providers"]}

    assert resolver_calls == ["nous", "openrouter"]  # no models, no check
    assert rows["nous"]["authenticated"] is False
    assert rows["nous"]["auth_error"] == "Hermes is not logged into Nous Portal."
    assert rows["openrouter"]["authenticated"] is True
    assert "auth_error" not in rows["openrouter"]
    assert rows["custom"]["authenticated"] is True


def test_session_lock_names_model_and_provider(resolver_calls, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    lock = {"browser_model_lock": {"model": "anthropic/claude-sonnet-5", "provider": "openrouter"}}
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
    con.execute("INSERT INTO sessions VALUES (?, ?)", ("s1", json.dumps(lock)))
    con.commit()
    con.close()

    payload = model_options.build("s1")

    assert payload["session_model"] == "anthropic/claude-sonnet-5"
    assert payload["session_provider"] == "openrouter"


def test_unreadable_session_config_is_ignored(resolver_calls, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
    con.execute("INSERT INTO sessions VALUES (?, ?)", ("s1", "not json"))
    con.commit()
    con.close()

    for session_id in ("s1", "missing"):
        payload = model_options.build(session_id)
        assert "session_model" not in payload and "session_provider" not in payload

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))  # no state.db at all
    assert "session_model" not in model_options.build("s1")
