import asyncio
import json

import pytest

from hermes_app import board, routes


def _board(**block):
    block = {"type": "note", "text": "hi", **block}
    return {"sections": [{"title": "S", "blocks": [block]}]}


def test_job_id_parsing():
    assert board.job_id_of("cron_a1b2c3d4_20261122_075000") == "a1b2c3d4"
    assert board.job_id_of("20261121_201455_ab12") is None
    assert board.job_id_of("") is None


@pytest.mark.parametrize("raw,field", [
    ({"sections": []}, "board.sections"),
    (_board(type="gauge"), "sections[0].blocks[0].type"),
    (_board(type="session", session_id="x", title="t"), "blocks[0].reason"),
    (_board(tone="red"), "blocks[0].tone"),
    (_board(action={"open": "tab", "tab": "nope"}), "action.tab"),
    ({"sections": [{"title": "S", "blocks": [{"type": "note", "text": "x"}] * 41}]}, "40 blocks"),
])
def test_validation_names_the_field(raw, field):
    with pytest.raises(board.BoardError, match=field.replace("[", r"\[").replace("]", r"\]")):
        board.validate(raw)


def test_publish_stamps_and_cuts(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path))
    out = json.loads(board.handle_publish({"board": _board(text="x" * 2000)}, session_id="cron_a1b2c3d4_20261122_075000"))
    assert out == {"ok": True, "blocks": 1}
    got = board.read()
    assert got["job_id"] == "a1b2c3d4" and got["session_id"] == "cron_a1b2c3d4_20261122_075000"
    assert got["generated_at"]
    assert len(got["sections"][0]["blocks"][0]["text"]) == 1200
    bad = json.loads(board.handle_publish({"board": {"sections": "x"}}))
    assert bad["ok"] is False and "board.sections" in bad["error"]


def test_route_null_then_board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path))
    body = lambda: json.loads(asyncio.run(routes.get_board(None)).text)
    assert body() == {"board": None}
    board.publish(_board())
    assert body()["board"]["sections"][0]["title"] == "S"
