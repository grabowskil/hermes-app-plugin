import json

import pytest

from hermes_app import budget

SID = "api_1_abc"


@pytest.fixture(autouse=True)
def _clear(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    budget._modes.clear()
    budget._counts.clear()
    budget._limits = None


def call(tool="terminal", sid=SID, turn="t1"):
    return budget.on_pre_tool_call(tool_name=tool, session_id=sid, turn_id=turn, args={})


def note(sid=SID, turn="t1"):
    return budget.on_transform_tool_result(tool_name="terminal", session_id=sid, turn_id=turn, result="ok")


def test_soft_note_from_twelfth_call():
    for i in range(1, 12):
        assert call() is None
        assert note() is None, i
    assert call() is None
    assert note() == "ok\n\n" + budget.NOTE


def test_blocks_after_hard_limit():
    for _ in range(25):
        assert call() is None
    blocked = call()
    assert blocked["action"] == "block" and blocked["message"]
    assert call(turn="t2") is None  # a new turn starts over


def test_web_cap_blocks_web_only():
    for _ in range(4):
        assert call("web_search") is None
    assert call("browser_navigate")["action"] == "block"
    assert call("web_extract")["action"] == "block"
    assert call("terminal") is None


def test_other_platforms_untouched():
    for _ in range(40):
        assert call(sid="20261027_abc") is None
    assert note(sid="20261027_abc") is None


def test_deep_mode_limits():
    assert budget.set_mode(SID, "deep") == "deep"
    for _ in range(29):
        call()
    assert note() is None
    call()
    assert note() is not None
    for _ in range(30):
        call()
    assert call()["action"] == "block"


def test_mode_default_and_validation():
    assert budget.get_mode(SID) == "quick"
    with pytest.raises(ValueError):
        budget.set_mode(SID, "turbo")


def test_override_file(tmp_path):
    (tmp_path / "hermes_app").mkdir()
    (tmp_path / "hermes_app" / "budget.json").write_text(json.dumps({"quick": {"hard": 2}}))
    call()
    call()
    assert call()["action"] == "block"


def test_counters_capped():
    for i in range(budget.CAP + 20):
        call(turn=f"t{i}")
    assert len(budget._counts) == budget.CAP
