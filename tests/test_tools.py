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


@pytest.fixture
def capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture _request calls and return a stub success response."""
    captured: dict[str, Any] = {}

    async def _stub(method: str, path: str, *, params: Any = None, body: Any = None) -> Any:
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(server, "_request", _stub)
    return captured


# ── Existing validation tests (preserved unchanged) ───────────────────────────

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


# ── A. Pagination + summary mode ──────────────────────────────────────────────

async def test_list_issues_summary_mode_projects_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary=True strips all keys except the compact set."""
    async def _stub(*a: Any, **kw: Any) -> Any:
        return [
            {
                "id": "1", "identifier": "CY-1", "title": "T", "status": "todo",
                "priority": "low", "assigneeAgentId": None, "projectId": None,
                "goalId": None, "parentId": None, "updatedAt": "2024-01-01",
                "longFieldA": "should be dropped", "longFieldB": 99,
            }
        ]

    monkeypatch.setattr(server, "_request", _stub)
    result = await server.list_issues(summary=True)
    assert isinstance(result, list)
    assert "longFieldA" not in result[0]
    assert "longFieldB" not in result[0]
    assert result[0]["title"] == "T"
    assert result[0]["identifier"] == "CY-1"


async def test_list_issues_full_mode_preserves_all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary=False returns the full object."""
    async def _stub(*a: Any, **kw: Any) -> Any:
        return [{"id": "1", "title": "T", "status": "todo", "extraField": "keep"}]

    monkeypatch.setattr(server, "_request", _stub)
    result = await server.list_issues(summary=False)
    assert isinstance(result, list)
    assert result[0]["extraField"] == "keep"


async def test_list_issues_offset_sent_in_params(capture_request: dict[str, Any]) -> None:
    await server.list_issues(offset=100)
    assert capture_request["params"]["offset"] == 100


async def test_list_issues_limit_clamped(capture_request: dict[str, Any]) -> None:
    await server.list_issues(limit=999)
    assert capture_request["params"]["limit"] == 200


async def test_list_goals_summary_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stub(*a: Any, **kw: Any) -> Any:
        return [{"id": "g1", "title": "G", "status": "active", "parentId": None,
                 "level": 0, "updatedAt": "2024-01-01", "extraGoalField": "drop"}]

    monkeypatch.setattr(server, "_request", _stub)
    result = await server.list_goals(summary=True)
    assert isinstance(result, list)
    assert "extraGoalField" not in result[0]
    assert result[0]["title"] == "G"


async def test_list_goals_pagination_params(capture_request: dict[str, Any]) -> None:
    await server.list_goals(limit=10, offset=20)
    assert capture_request["params"]["limit"] == 10
    assert capture_request["params"]["offset"] == 20


async def test_list_activity_summary_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stub(*a: Any, **kw: Any) -> Any:
        return [{"id": "a1", "type": "checkout", "agentId": "u1",
                 "issueId": "i1", "goalId": None, "createdAt": "2024-01-01",
                 "verboseField": "drop me"}]

    monkeypatch.setattr(server, "_request", _stub)
    result = await server.list_activity(summary=True)
    assert isinstance(result, list)
    assert "verboseField" not in result[0]
    assert result[0]["type"] == "checkout"


async def test_list_activity_offset_param(capture_request: dict[str, Any]) -> None:
    await server.list_activity(offset=50)
    assert capture_request["params"]["offset"] == 50


async def test_compact_handles_paginated_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """_compact also works when the API wraps results in a 'data' key."""
    async def _stub(*a: Any, **kw: Any) -> Any:
        return {
            "data": [{"id": "1", "title": "T", "status": "todo", "extra": "drop"}],
            "total": 1,
        }

    monkeypatch.setattr(server, "_request", _stub)
    result = await server.list_issues(summary=True)
    assert isinstance(result, dict)
    assert "extra" not in result["data"][0]
    assert result["data"][0]["title"] == "T"
    assert result["total"] == 1


# ── A + B. list_issues new filters ────────────────────────────────────────────

async def test_list_issues_goal_id_filter(capture_request: dict[str, Any]) -> None:
    goal = "123e4567-e89b-42d3-a456-426614174000"
    await server.list_issues(goal_id=goal)
    assert capture_request["params"]["goalId"] == goal


async def test_list_issues_goal_id_rejects_non_uuid(no_http: None) -> None:
    result = await server.list_issues(goal_id="not-a-uuid")
    assert result["isError"] is True


async def test_list_issues_parent_issue_id_filter(capture_request: dict[str, Any]) -> None:
    await server.list_issues(parent_issue_id="CY-10")
    assert capture_request["params"]["parentIssueId"] == "CY-10"


async def test_list_issues_parent_issue_id_rejects_injection(no_http: None) -> None:
    result = await server.list_issues(parent_issue_id="../x")
    assert result["isError"] is True


async def test_list_issues_assignee_uuid_validated(no_http: None) -> None:
    result = await server.list_issues(assignee_agent_id="not-a-uuid")
    assert result["isError"] is True


# ── B. create_issue new fields ────────────────────────────────────────────────

async def test_create_issue_labels_csv_split(capture_request: dict[str, Any]) -> None:
    await server.create_issue(title="T", labels="bug, enhancement , security")
    assert capture_request["body"]["labels"] == ["bug", "enhancement", "security"]


async def test_create_issue_with_valid_status(capture_request: dict[str, Any]) -> None:
    await server.create_issue(title="T", status="in_progress")
    assert capture_request["body"]["status"] == "in_progress"


async def test_create_issue_rejects_invalid_status(no_http: None) -> None:
    result = await server.create_issue(title="T", status="shipped")
    assert result["isError"] is True
    assert "shipped" in result["message"]


async def test_create_issue_work_mode_sent(capture_request: dict[str, Any]) -> None:
    await server.create_issue(title="T", work_mode="autonomous")
    assert capture_request["body"]["workMode"] == "autonomous"


async def test_create_issue_goal_id_sent(capture_request: dict[str, Any]) -> None:
    gid = "123e4567-e89b-42d3-a456-426614174000"
    await server.create_issue(title="T", goal_id=gid)
    assert capture_request["body"]["goalId"] == gid


async def test_create_issue_goal_id_rejects_non_uuid(no_http: None) -> None:
    result = await server.create_issue(title="T", goal_id="bad-id")
    assert result["isError"] is True


async def test_create_issue_assignee_uuid_validated(no_http: None) -> None:
    result = await server.create_issue(title="T", assignee_agent_id="not-uuid")
    assert result["isError"] is True


async def test_create_issue_rejects_invalid_priority(no_http: None) -> None:
    result = await server.create_issue(title="T", priority="instant")
    assert result["isError"] is True


# ── B. update_issue new fields ────────────────────────────────────────────────

async def test_update_issue_labels_csv_split(capture_request: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-1", labels="alpha, beta")
    assert capture_request["body"]["labels"] == ["alpha", "beta"]


async def test_update_issue_unlinks_parent(capture_request: dict[str, Any]) -> None:
    """Sentinel 'null' must produce parentIssueId: null (None) in the body."""
    await server.update_issue(issue_id="CY-1", parent_issue_id="null")
    assert "parentIssueId" in capture_request["body"]
    assert capture_request["body"]["parentIssueId"] is None


async def test_update_issue_sets_parent(capture_request: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-1", parent_issue_id="CY-5")
    assert capture_request["body"]["parentIssueId"] == "CY-5"


async def test_update_issue_unlinks_goal(capture_request: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-1", goal_id="null")
    assert capture_request["body"]["goalId"] is None


async def test_update_issue_sets_goal(capture_request: dict[str, Any]) -> None:
    gid = "123e4567-e89b-42d3-a456-426614174000"
    await server.update_issue(issue_id="CY-1", goal_id=gid)
    assert capture_request["body"]["goalId"] == gid


async def test_update_issue_goal_rejects_non_uuid(no_http: None) -> None:
    result = await server.update_issue(issue_id="CY-1", goal_id="bad")
    assert result["isError"] is True


async def test_update_issue_unlinks_project(capture_request: dict[str, Any]) -> None:
    await server.update_issue(issue_id="CY-1", project_id="null")
    assert capture_request["body"]["projectId"] is None


async def test_update_issue_assignee_uuid_validated(no_http: None) -> None:
    result = await server.update_issue(issue_id="CY-1", assignee_agent_id="bad")
    assert result["isError"] is True


async def test_update_issue_no_fields_error(no_http: None) -> None:
    result = await server.update_issue(issue_id="CY-1")
    assert result["isError"] is True


# ── B. update_goal new fields ─────────────────────────────────────────────────

async def test_update_goal_status_sent(capture_request: dict[str, Any]) -> None:
    gid = "123e4567-e89b-42d3-a456-426614174000"
    await server.update_goal(goal_id=gid, status="completed")
    assert capture_request["body"]["status"] == "completed"


async def test_update_goal_unlinks_parent(capture_request: dict[str, Any]) -> None:
    gid = "123e4567-e89b-42d3-a456-426614174000"
    await server.update_goal(goal_id=gid, parent_id="null")
    assert "parentId" in capture_request["body"]
    assert capture_request["body"]["parentId"] is None


async def test_update_goal_sets_parent(capture_request: dict[str, Any]) -> None:
    gid = "123e4567-e89b-42d3-a456-426614174000"
    pid = "223e4567-e89b-42d3-a456-426614174000"
    await server.update_goal(goal_id=gid, parent_id=pid)
    assert capture_request["body"]["parentId"] == pid


async def test_update_goal_parent_rejects_non_uuid(no_http: None) -> None:
    gid = "123e4567-e89b-42d3-a456-426614174000"
    result = await server.update_goal(goal_id=gid, parent_id="not-uuid")
    assert result["isError"] is True


async def test_update_goal_no_fields_error(no_http: None) -> None:
    gid = "123e4567-e89b-42d3-a456-426614174000"
    result = await server.update_goal(goal_id=gid)
    assert result["isError"] is True


# ── B. create_goal parent_id ──────────────────────────────────────────────────

async def test_create_goal_with_parent(capture_request: dict[str, Any]) -> None:
    pid = "123e4567-e89b-42d3-a456-426614174000"
    await server.create_goal(title="Sub-goal", parent_id=pid)
    assert capture_request["body"]["parentId"] == pid


async def test_create_goal_parent_rejects_non_uuid(no_http: None) -> None:
    result = await server.create_goal(title="T", parent_id="bad")
    assert result["isError"] is True


# ── C. New tools registered & UUID-validated ─────────────────────────────────

async def test_get_goal_registered() -> None:
    tools = await server.mcp.list_tools()
    assert "get_goal" in {t.name for t in tools}


async def test_get_goal_rejects_non_uuid(no_http: None) -> None:
    result = await server.get_goal(goal_id="not-a-uuid")
    assert result["isError"] is True


async def test_get_goal_rejects_injection(no_http: None) -> None:
    result = await server.get_goal(goal_id="../x")
    assert result["isError"] is True


async def test_list_projects_registered() -> None:
    tools = await server.mcp.list_tools()
    assert "list_projects" in {t.name for t in tools}


async def test_get_project_registered() -> None:
    tools = await server.mcp.list_tools()
    assert "get_project" in {t.name for t in tools}


async def test_get_project_rejects_non_uuid(no_http: None) -> None:
    result = await server.get_project(project_id="not-a-uuid")
    assert result["isError"] is True


async def test_list_comments_registered() -> None:
    tools = await server.mcp.list_tools()
    assert "list_comments" in {t.name for t in tools}


async def test_list_comments_rejects_injection(no_http: None) -> None:
    result = await server.list_comments(issue_id="../x")
    assert result["isError"] is True


async def test_list_comments_accepts_valid_ref(capture_request: dict[str, Any]) -> None:
    await server.list_comments(issue_id="CY-42")
    assert "/comments" in capture_request["path"]


# ── D. Robustness: _opt_uuid helper ──────────────────────────────────────────

def test_opt_uuid_returns_none_for_empty() -> None:
    assert server._opt_uuid("", "x") is None
    assert server._opt_uuid("   ", "x") is None


def test_opt_uuid_canonicalises_uuid() -> None:
    raw = "123E4567-E89B-42D3-A456-426614174000"
    expected = "123e4567-e89b-42d3-a456-426614174000"
    assert server._opt_uuid(raw, "x") == expected


def test_opt_uuid_raises_for_bad_value() -> None:
    with pytest.raises(ValueError):
        server._opt_uuid("not-uuid", "x")


# ── D. Robustness: list_activity agent_id UUID validation ─────────────────────

async def test_list_activity_agent_id_uuid_validated(no_http: None) -> None:
    result = await server.list_activity(agent_id="bad-id")
    assert result["isError"] is True
