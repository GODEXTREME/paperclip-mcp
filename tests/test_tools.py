"""Tests for tool registration and input validation (no HTTP traffic)."""

from typing import Any

import pytest

from paperclip_mcp import server


async def test_delete_issue_is_not_registered() -> None:
    tools = await server.mcp.list_tools()
    assert "delete_issue" not in {tool.name for tool in tools}


async def test_no_tool_uses_http_delete() -> None:
    import inspect

    source = inspect.getsource(server)
    assert '"DELETE"' not in source and "'DELETE'" not in source


@pytest.fixture
def no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outgoing HTTP attempt fail the test."""

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tool performed an HTTP request despite invalid input")

    monkeypatch.setattr(server, "_request", _boom)


async def test_list_issues_rejects_invalid_status(no_http: None) -> None:
    result = await server.list_issues(status="todo,bogus")
    assert result["isError"] is True
    assert "bogus" in result["message"]


async def test_list_issues_rejects_empty_status(no_http: None) -> None:
    result = await server.list_issues(status=" , ")
    assert result["isError"] is True


async def test_update_issue_rejects_invalid_status(no_http: None) -> None:
    result = await server.update_issue(issue_id="CY-42", status="nope")
    assert result["isError"] is True
    assert "nope" in result["message"]


async def test_update_issue_rejects_invalid_priority(no_http: None) -> None:
    result = await server.update_issue(issue_id="CY-42", priority="asap")
    assert result["isError"] is True


async def test_update_issue_rejects_malicious_issue_id(no_http: None) -> None:
    result = await server.update_issue(issue_id="../companies/x", status="todo")
    assert result["isError"] is True


async def test_list_approvals_rejects_invalid_status(no_http: None) -> None:
    result = await server.list_approvals(status="whatever")
    assert result["isError"] is True


async def test_get_issue_rejects_path_injection(no_http: None) -> None:
    for bad in ("../x", "a/b", "a?x=1", "", "a" * 65):
        result = await server.get_issue(issue_id=bad)
        assert result["isError"] is True, f"accepted malicious issue_id: {bad!r}"


async def test_uuid_tools_reject_non_uuid_ids(no_http: None) -> None:
    assert (await server.invoke_agent_heartbeat(agent_id="../x"))["isError"] is True
    assert (await server.update_goal(goal_id="nope", title="t"))["isError"] is True
    assert (await server.approve(approval_id="nope"))["isError"] is True
    assert (await server.reject(approval_id="nope"))["isError"] is True
    assert (await server.request_approval_revision(approval_id="nope", comment="c"))[
        "isError"
    ] is True
