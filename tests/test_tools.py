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


# ── Hierarchy / linkage fields ──────────────────────────────────────────────────

_UUID_A = "11111111-1111-4111-8111-111111111111"
_UUID_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the outgoing request instead of performing it."""
    calls: dict[str, Any] = {}

    async def _record(
        method: str,
        path: str,
        *,
        params: Any = None,
        body: Any = None,
    ) -> Any:
        calls.update(method=method, path=path, params=params, body=body)
        return {"ok": True}

    monkeypatch.setattr(server, "_request", _record)
    return calls


async def test_create_issue_rejects_invalid_priority(no_http: None) -> None:
    result = await server.create_issue(title="t", priority="asap")
    assert result["isError"] is True
    assert "asap" in result["message"]


async def test_create_issue_rejects_non_uuid_linkage(no_http: None) -> None:
    for field in ("project_id", "parent_issue_id", "goal_id", "assignee_agent_id"):
        result = await server.create_issue(title="t", **{field: "not-a-uuid"})
        assert result["isError"] is True, f"accepted non-UUID {field}"


async def test_create_issue_sends_goal_id(capture: dict[str, Any]) -> None:
    await server.create_issue(title="t", goal_id=_UUID_A, project_id=_UUID_B)
    assert capture["method"] == "POST"
    assert capture["body"]["goalId"] == _UUID_A
    assert capture["body"]["projectId"] == _UUID_B


async def test_update_issue_rejects_non_uuid_linkage(no_http: None) -> None:
    for field in ("project_id", "parent_issue_id", "goal_id"):
        result = await server.update_issue(issue_id="CY-42", **{field: "nope"})
        assert result["isError"] is True, f"accepted non-UUID {field}"


async def test_update_issue_sends_linkage(capture: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-42", goal_id=_UUID_A, project_id=_UUID_B)
    assert capture["method"] == "PATCH"
    assert capture["body"]["goalId"] == _UUID_A
    assert capture["body"]["projectId"] == _UUID_B


async def test_create_goal_sends_parent_id(capture: dict[str, Any]) -> None:
    await server.create_goal(title="sub", parent_id=_UUID_A, level="project")
    assert capture["body"]["parentId"] == _UUID_A
    assert capture["body"]["level"] == "project"


async def test_create_goal_rejects_non_uuid_parent(no_http: None) -> None:
    result = await server.create_goal(title="sub", parent_id="not-a-uuid")
    assert result["isError"] is True


async def test_update_goal_sends_parent_id(capture: dict[str, Any]) -> None:
    await server.update_goal(goal_id=_UUID_A, parent_id=_UUID_B)
    assert capture["method"] == "PATCH"
    assert capture["body"] == {"parentId": _UUID_B}


async def test_update_goal_rejects_non_uuid_parent(no_http: None) -> None:
    result = await server.update_goal(goal_id=_UUID_A, parent_id="nope")
    assert result["isError"] is True
