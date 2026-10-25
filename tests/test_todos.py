import json

import pytest

from hermes_app import todos

TODO_RESULT = json.dumps({"todos": [{"id": "1", "content": "Read", "status": "completed"}], "revision": 1})


@pytest.fixture(autouse=True)
def _clear():
    todos._latest.clear()


def test_stores_valid_todo_result():
    todos.on_post_tool_call(tool_name="todo_list", session_id="s1", result=TODO_RESULT)
    assert todos.latest("s1") == [{"id": "1", "content": "Read", "status": "completed"}]


def test_accepts_legacy_tool_name():
    todos.on_post_tool_call(tool_name="todo", session_id="s1", result=TODO_RESULT)
    assert todos.latest("s1") is not None


def test_ignores_invalid_results():
    todos.on_post_tool_call(tool_name="web_search", session_id="s1", result=TODO_RESULT)
    todos.on_post_tool_call(tool_name="todo_list", session_id="s1", result="not json")
    todos.on_post_tool_call(tool_name="todo_list", session_id="s1", result="[1, 2]")
    todos.on_post_tool_call(tool_name="todo_list", session_id="s1", result='{"revision": 2}')
    todos.on_post_tool_call(tool_name="todo_list", session_id="", result=TODO_RESULT)
    assert todos.latest("s1") is None


def test_latest_replaces_previous_list():
    todos.on_post_tool_call(tool_name="todo_list", session_id="s1", result=TODO_RESULT)
    todos.on_post_tool_call(tool_name="todo_list", session_id="s1", result='{"todos": []}')
    assert todos.latest("s1") == []


def test_cap_drops_oldest():
    for i in range(todos.CAP + 1):
        todos.on_post_tool_call(tool_name="todo_list", session_id=f"s{i}", result=TODO_RESULT)
    assert len(todos._latest) == todos.CAP
    assert todos.latest("s0") is None
