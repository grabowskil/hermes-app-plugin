import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_app import activity, budget, routes

KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}


class FakeAdapter:
    _api_key = KEY

    def __init__(self):
        self._run_statuses = {}


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    activity._state.clear()
    budget._modes.clear()
    monkeypatch.setattr(activity, "_adapter", None)


def _turn(session="s1", turn="t1"):
    activity.on_pre_llm_call(session_id=session, turn_id=turn)
    activity.on_post_api_request(session_id=session, turn_id=turn, usage={"prompt_tokens": 100, "output_tokens": 7})
    activity.on_post_tool_call(session_id=session, turn_id=turn, tool_name="x")


def test_turn_counts_and_end():
    _turn()
    s = activity.snapshot("s1")
    assert (s["active"], s["kind"], s["run_id"]) == (True, "background", None)
    assert (s["api_calls"], s["tool_calls"], s["tokens_in"], s["tokens_out"]) == (1, 1, 100, 7)
    assert s["mode"] == "quick" and s["started_at"] <= s["updated_at"]
    activity.on_turn_end(session_id="s1", turn_id="t1")
    s = activity.snapshot("s1")
    assert (s["active"], s["kind"], s["api_calls"]) == (False, None, 1)


def test_new_turn_resets_counters():
    _turn()
    activity.on_turn_end(session_id="s1", turn_id="t1")
    activity.on_pre_llm_call(session_id="s1", turn_id="t2")
    s = activity.snapshot("s1")
    assert (s["active"], s["api_calls"], s["tool_calls"]) == (True, 0, 0)


def test_idle_after_90_seconds():
    _turn()
    activity._state["s1"]["updated_at"] = time.time() - activity.IDLE_SECONDS - 1
    assert activity.snapshot("s1")["active"] is False


def test_fork_after_turn_end_is_a_new_turn_and_stale_end_is_ignored():
    _turn()
    activity.on_turn_end(session_id="s1", turn_id="t1")
    activity.on_pre_api_request(session_id="s1", turn_id="fork")  # background review fork, no pre_llm_call
    activity.on_turn_end(session_id="s1", turn_id="t1")  # parent's late on_session_end
    s = activity.snapshot("s1")
    assert (s["active"], s["kind"], s["api_calls"]) == (True, "background", 0)


def test_live_run_id_makes_kind_run():
    adapter = FakeAdapter()
    adapter._run_statuses = {
        "run_old": {"session_id": "s1", "status": "completed", "created_at": 3},
        "run_a": {"session_id": "s1", "status": "running", "created_at": 1},
        "run_b": {"session_id": "s1", "status": "waiting_for_approval", "created_at": 2},
        "run_c": {"session_id": "other", "status": "running", "created_at": 9},
    }
    activity.bind(adapter)
    s = activity.snapshot("s1")
    assert (s["active"], s["kind"], s["run_id"]) == (True, "run", "run_b")
    assert activity.snapshot("none")["run_id"] is None


def test_bad_input_never_raises_and_cap_drops_oldest():
    activity.on_post_api_request(session_id=None, usage="x")
    activity.on_post_api_request(session_id="s1", usage={"prompt_tokens": "many"})
    assert activity.snapshot("s1")["tokens_in"] == 0
    for i in range(activity.CAP + 5):
        activity.on_pre_api_request(session_id=f"c{i}")
    assert len(activity._state) == activity.CAP and "c0" not in activity._state


def _run(fn):
    async def main():
        app = web.Application()
        routes.wire(app, FakeAdapter())
        async with TestClient(TestServer(app)) as client:
            return await fn(client)

    return asyncio.run(main())


def test_activity_and_mode_routes():
    async def go(c):
        put = await c.put("/app/v1/sessions/s1/mode", json={"mode": "deep"}, headers=AUTH)
        bad = await c.put("/app/v1/sessions/s1/mode", json={"mode": "turbo"}, headers=AUTH)
        none = await c.put("/app/v1/sessions/s1/mode", data="x", headers=AUTH)
        act = await c.get("/app/v1/sessions/s1/activity", headers=AUTH)
        denied = await c.get("/app/v1/sessions/s1/activity")
        return put.status, await put.json(), bad.status, none.status, await act.json(), denied.status

    put, body, bad, none, act, denied = _run(go)
    assert (put, body, bad, none, denied) == (200, {"mode": "deep"}, 400, 400, 401)
    assert act["mode"] == "deep" and act["active"] is False
