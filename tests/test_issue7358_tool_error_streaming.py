import json
import pytest
from unittest.mock import MagicMock
from api.streaming import _is_tool_error, _tool_result_snippet
from api.gateway_chat import _gateway_tool_progress_event


def test_is_tool_error_with_structured_failure_dict():
    # 1. Object result containing success: false and error emits is_error: true
    res = {
        "success": False,
        "error": "HTTP 433: usage allowance exceeded"
    }
    assert _is_tool_error(res) is True


def test_is_tool_error_with_json_string_failure():
    # 2. JSON-string result with the same fields emits is_error: true
    res_str = json.dumps({
        "success": False,
        "error": "HTTP 433: usage allowance exceeded"
    })
    assert _is_tool_error(res_str) is True


def test_is_tool_error_with_successful_result():
    # 3. Successful result emits is_error: false
    res = {
        "success": True,
        "output": "Files listed successfully: file1.txt, file2.txt"
    }
    assert _is_tool_error(res) is False

    res_str = json.dumps(res)
    assert _is_tool_error(res_str) is False


def test_is_tool_error_does_not_misclassify_text_containing_word_error():
    # 4. Legitimate successful data containing the word 'error' is not misclassified
    res_dict = {
        "success": True,
        "output": "Analyzed logs: found 0 error codes, 10 successes."
    }
    assert _is_tool_error(res_dict) is False

    # Plain text containing 'error' without structured failure indicator
    assert _is_tool_error("Error handling pattern documentation complete.") is False


def test_is_tool_error_various_error_indicators():
    # Status error/failed
    assert _is_tool_error({"status": "failed", "message": "timed out"}) is True
    assert _is_tool_error({"status": "error", "message": "boom"}) is True
    assert _is_tool_error({"status": "failure", "message": "bad"}) is True

    # Exit codes
    assert _is_tool_error({"exit_code": 1, "output": "command failed"}) is True
    assert _is_tool_error({"returncode": 127, "output": "not found"}) is True
    assert _is_tool_error({"exit_status": 2, "output": "usage error"}) is True
    assert _is_tool_error({"exit_code": 0, "output": "all good"}) is False

    # Explicit is_error
    assert _is_tool_error({"is_error": True, "output": "failed"}) is True
    assert _is_tool_error({"is_error": False, "output": "syntax error on line 3"}) is False

    # Exception
    assert _is_tool_error(RuntimeError("subprocess crashed")) is True


def test_tool_result_snippet_prioritizes_error_message():
    # 5. Error previews remain bounded and prioritize error description
    res = {
        "success": False,
        "error": "HTTP 433: usage allowance exceeded",
        "output": "Partial unhelpful dump"
    }
    snippet = _tool_result_snippet(res)
    assert snippet == "HTTP 433: usage allowance exceeded"


def test_gateway_run_event_consistent_is_error():
    # 7. Gateway event translation produces consistent is_error semantics
    payload = {
        "event": "tool.completed",
        "name": "web_search",
        "toolCallId": "call-123",
        "status": "completed",
        "result": {
            "success": False,
            "error": "Rate limit reached"
        }
    }
    event_type, data = _gateway_tool_progress_event(payload)
    assert event_type == "tool_complete"
    assert data["is_error"] is True
    assert data["tid"] == "call-123"

    payload_ok = {
        "event": "tool.completed",
        "name": "web_search",
        "toolCallId": "call-124",
        "status": "completed",
        "result": {
            "success": True,
            "output": "Search result about error handling in Python"
        }
    }
    event_type_ok, data_ok = _gateway_tool_progress_event(payload_ok)
    assert event_type_ok == "tool_complete"
    assert data_ok["is_error"] is False
