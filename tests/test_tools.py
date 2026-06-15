"""Tests for tool registration and input validation (no HTTP traffic)."""

from typing import Any

import pytest

from paperclip_mcp import server

_UUID_A = "11111111-1111-4111-8111-111111111111"
_UUID_B = "22222222-2222-4222-8222-222222222222"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outgoing HTTP attempt fail the test."""

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tool performed an HTTP request despite invalid input")

    monkeypatch.setattr(server, "_request", _boom)


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


# ── Tool registration ──────────────────────────────────────────────────────────


async def test_delete_issue_is_not_registered() -> None:
    tools = await server.mcp.list_tools()
    assert "delete_issue" not in {tool.name for tool in tools}


async def test_no_tool_uses_http_delete() -> None:
    import inspect

    source = inspect.getsource(server)
    assert '"DELETE"' not in source and "'DELETE'" not in source


async def test_new_tools_are_registered() -> None:
    tools = {tool.name for tool in await server.mcp.list_tools()}
    for name in ("get_goal", "list_projects", "get_project", "list_comments"):
        assert name in tools, f"tool not registered: {name}"


# ── list_issues ────────────────────────────────────────────────────────────────


async def test_list_issues_rejects_invalid_status(no_http: None) -> None:
    result = await server.list_issues(status="todo,bogus")
    assert result["isError"] is True
    assert "bogus" in result["message"]


async def test_list_issues_rejects_empty_status(no_http: None) -> None:
    result = await server.list_issues(status=" , ")
    assert result["isError"] is True


async def test_list_issues_rejects_invalid_assignee(no_http: None) -> None:
    result = await server.list_issues(assignee_agent_id="not-a-uuid")
    assert result["isError"] is True


async def test_list_issues_rejects_invalid_goal_id(no_http: None) -> None:
    result = await server.list_issues(goal_id="not-a-uuid")
    assert result["isError"] is True


async def test_list_issues_rejects_invalid_parent_issue_id(no_http: None) -> None:
    result = await server.list_issues(parent_issue_id="not-a-uuid")
    assert result["isError"] is True


async def test_list_issues_pagination(capture: dict[str, Any]) -> None:
    await server.list_issues(limit=10, offset=20)
    assert capture["params"]["limit"] == 10
    assert capture["params"]["offset"] == 20


async def test_list_issues_sends_goal_id_filter(capture: dict[str, Any]) -> None:
    await server.list_issues(goal_id=_UUID_A)
    assert capture["params"]["goalId"] == _UUID_A


async def test_list_issues_sends_parent_issue_id_filter(capture: dict[str, Any]) -> None:
    await server.list_issues(parent_issue_id=_UUID_A)
    assert capture["params"]["parentIssueId"] == _UUID_A


async def test_list_issues_summary_mode(capture: dict[str, Any]) -> None:
    capture["_raw"] = [{"id": "x", "title": "t", "extraField": "drop"}]

    async def _fake(method: str, path: str, *, params: Any = None, body: Any = None) -> Any:
        return capture["_raw"]

    import unittest.mock

    with unittest.mock.patch.object(server, "_request", _fake):
        result = await server.list_issues(summary=True)
    assert isinstance(result, list)
    assert result[0] == {"id": "x", "title": "t"}


# ── get_issue ─────────────────────────────────────────────────────────────────


async def test_get_issue_rejects_path_injection(no_http: None) -> None:
    for bad in ("../x", "a/b", "a?x=1", "", "a" * 65):
        result = await server.get_issue(issue_id=bad)
        assert result["isError"] is True, f"accepted malicious issue_id: {bad!r}"


# ── create_issue ───────────────────────────────────────────────────────────────


async def test_create_issue_rejects_invalid_priority(no_http: None) -> None:
    result = await server.create_issue(title="t", priority="asap")
    assert result["isError"] is True
    assert "asap" in result["message"]


async def test_create_issue_rejects_invalid_status(no_http: None) -> None:
    result = await server.create_issue(title="t", status="invalid")
    assert result["isError"] is True
    assert "invalid" in result["message"]


async def test_create_issue_rejects_non_uuid_linkage(no_http: None) -> None:
    for field in ("project_id", "parent_issue_id", "goal_id", "assignee_agent_id"):
        result = await server.create_issue(title="t", **{field: "not-a-uuid"})
        assert result["isError"] is True, f"accepted non-UUID {field}"


async def test_create_issue_sends_goal_id(capture: dict[str, Any]) -> None:
    await server.create_issue(title="t", goal_id=_UUID_A, project_id=_UUID_B)
    assert capture["method"] == "POST"
    assert capture["body"]["goalId"] == _UUID_A
    assert capture["body"]["projectId"] == _UUID_B


async def test_create_issue_sends_labels(capture: dict[str, Any]) -> None:
    await server.create_issue(title="t", labels="bug, backend, urgent")
    assert capture["body"]["labels"] == ["bug", "backend", "urgent"]


async def test_create_issue_sends_status(capture: dict[str, Any]) -> None:
    await server.create_issue(title="t", status="in_progress")
    assert capture["body"]["status"] == "in_progress"


async def test_create_issue_sends_work_mode(capture: dict[str, Any]) -> None:
    await server.create_issue(title="t", work_mode="autonomous")
    assert capture["body"]["workMode"] == "autonomous"


# ── update_issue ───────────────────────────────────────────────────────────────


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


async def test_update_issue_rejects_non_uuid_linkage(no_http: None) -> None:
    for field in ("project_id", "parent_issue_id", "goal_id"):
        result = await server.update_issue(issue_id="CY-42", **{field: "nope"})
        assert result["isError"] is True, f"accepted non-UUID {field}"


async def test_update_issue_sends_linkage(capture: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-42", goal_id=_UUID_A, project_id=_UUID_B)
    assert capture["method"] == "PATCH"
    assert capture["body"]["goalId"] == _UUID_A
    assert capture["body"]["projectId"] == _UUID_B


async def test_update_issue_null_sentinel_goal_id(capture: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-42", goal_id="null")
    assert capture["body"]["goalId"] is None


async def test_update_issue_null_sentinel_project_id(capture: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-42", project_id="null")
    assert capture["body"]["projectId"] is None


async def test_update_issue_null_sentinel_parent_issue_id(capture: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-42", parent_issue_id="null")
    assert capture["body"]["parentIssueId"] is None


async def test_update_issue_sends_labels(capture: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-42", labels="feature, priority")
    assert capture["body"]["labels"] == ["feature", "priority"]


# ── list_goals ────────────────────────────────────────────────────────────────


async def test_list_goals_pagination(capture: dict[str, Any]) -> None:
    await server.list_goals(limit=25, offset=50)
    assert capture["params"]["limit"] == 25
    assert capture["params"]["offset"] == 50


async def test_list_goals_summary_mode(capture: dict[str, Any]) -> None:
    raw = [{"id": "x", "title": "g", "parentId": None, "status": "active", "extraField": "drop"}]

    async def _fake(method: str, path: str, *, params: Any = None, body: Any = None) -> Any:
        return raw

    import unittest.mock

    with unittest.mock.patch.object(server, "_request", _fake):
        result = await server.list_goals(summary=True)
    assert isinstance(result, list)
    assert "extraField" not in result[0]


# ── get_goal ─────────────────────────────────────────────────────────────────


async def test_get_goal_rejects_non_uuid(no_http: None) -> None:
    result = await server.get_goal(goal_id="not-a-uuid")
    assert result["isError"] is True


async def test_get_goal_makes_request(capture: dict[str, Any]) -> None:
    await server.get_goal(goal_id=_UUID_A)
    assert capture["method"] == "GET"
    assert _UUID_A in capture["path"]


# ── create_goal ────────────────────────────────────────────────────────────────


async def test_create_goal_sends_parent_id(capture: dict[str, Any]) -> None:
    await server.create_goal(title="sub", parent_id=_UUID_A, level="project")
    assert capture["body"]["parentId"] == _UUID_A
    assert capture["body"]["level"] == "project"


async def test_create_goal_rejects_non_uuid_parent(no_http: None) -> None:
    result = await server.create_goal(title="sub", parent_id="not-a-uuid")
    assert result["isError"] is True


async def test_create_goal_sends_project_id(capture: dict[str, Any]) -> None:
    await server.create_goal(title="g", project_id=_UUID_B)
    assert capture["body"]["projectId"] == _UUID_B


# ── update_goal ────────────────────────────────────────────────────────────────


async def test_update_goal_sends_parent_id(capture: dict[str, Any]) -> None:
    await server.update_goal(goal_id=_UUID_A, parent_id=_UUID_B)
    assert capture["method"] == "PATCH"
    assert capture["body"]["parentId"] == _UUID_B


async def test_update_goal_rejects_non_uuid_parent(no_http: None) -> None:
    result = await server.update_goal(goal_id=_UUID_A, parent_id="nope")
    assert result["isError"] is True


async def test_update_goal_null_sentinel_parent(capture: dict[str, Any]) -> None:
    await server.update_goal(goal_id=_UUID_A, parent_id="null")
    assert capture["body"]["parentId"] is None


async def test_update_goal_sends_status(capture: dict[str, Any]) -> None:
    await server.update_goal(goal_id=_UUID_A, status="completed")
    assert capture["body"]["status"] == "completed"


# ── list_projects / get_project ───────────────────────────────────────────────


async def test_list_projects_makes_request(capture: dict[str, Any]) -> None:
    await server.list_projects()
    assert capture["method"] == "GET"
    assert "projects" in capture["path"]


async def test_get_project_rejects_non_uuid(no_http: None) -> None:
    result = await server.get_project(project_id="not-a-uuid")
    assert result["isError"] is True


async def test_get_project_makes_request(capture: dict[str, Any]) -> None:
    await server.get_project(project_id=_UUID_A)
    assert capture["method"] == "GET"
    assert _UUID_A in capture["path"]


# ── list_comments ─────────────────────────────────────────────────────────────


async def test_list_comments_rejects_path_injection(no_http: None) -> None:
    for bad in ("../x", "a/b", "a?x=1", ""):
        result = await server.list_comments(issue_id=bad)
        assert result["isError"] is True, f"accepted malicious issue_id: {bad!r}"


async def test_list_comments_makes_request(capture: dict[str, Any]) -> None:
    await server.list_comments(issue_id="CY-42")
    assert capture["method"] == "GET"
    assert "comments" in capture["path"]


# ── list_activity ─────────────────────────────────────────────────────────────


async def test_list_activity_pagination(capture: dict[str, Any]) -> None:
    await server.list_activity(limit=5, offset=10)
    assert capture["params"]["limit"] == 5
    assert capture["params"]["offset"] == 10


async def test_list_activity_rejects_invalid_agent_id(no_http: None) -> None:
    result = await server.list_activity(agent_id="not-a-uuid")
    assert result["isError"] is True


# ── UUID tools ────────────────────────────────────────────────────────────────


async def test_uuid_tools_reject_non_uuid_ids(no_http: None) -> None:
    assert (await server.invoke_agent_heartbeat(agent_id="../x"))["isError"] is True
    assert (await server.update_goal(goal_id="nope", title="t"))["isError"] is True
    assert (await server.approve(approval_id="nope"))["isError"] is True
    assert (await server.reject(approval_id="nope"))["isError"] is True
    assert (await server.request_approval_revision(approval_id="nope", comment="c"))[
        "isError"
    ] is True


# ── list_approvals ────────────────────────────────────────────────────────────


async def test_list_approvals_rejects_invalid_status(no_http: None) -> None:
    result = await server.list_approvals(status="whatever")
    assert result["isError"] is True


# ── _compact helper ───────────────────────────────────────────────────────────


def test_compact_filters_list() -> None:
    keys: frozenset[str] = frozenset({"id", "title"})
    raw = [{"id": "1", "title": "t", "extra": "drop"}]
    result = server._compact(raw, keys)
    assert result == [{"id": "1", "title": "t"}]


def test_compact_handles_paginated_envelope() -> None:
    keys: frozenset[str] = frozenset({"id"})
    raw = {"data": [{"id": "x", "extra": "drop"}], "total": 1}
    result = server._compact(raw, keys)
    assert isinstance(result, dict)
    assert result["data"] == [{"id": "x"}]
    assert result["total"] == 1


def test_compact_passes_through_errors() -> None:
    err = {"isError": True, "message": "oops"}
    assert server._compact(err, frozenset()) is err
