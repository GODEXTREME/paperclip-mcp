#!/usr/bin/env python3
"""
paperclip-mcp — MCP server for the Paperclip AI agent orchestration platform.

Exposes Paperclip's REST API as MCP tools so that AI assistants can manage
issues, agents, goals, approvals, costs, and activity via natural language.

Documentation: https://github.com/godextreme/paperclip-mcp
MCP spec:      https://modelcontextprotocol.io

Configuration (environment variables):
    PAPERCLIP_API_KEY      Required. Agent API key — generate in Paperclip UI:
                           Settings → API Keys → New Key.
    PAPERCLIP_COMPANY_ID   Required. Company UUID shown in the Paperclip UI URL
                           when viewing your company: /companies/{uuid}.
    PAPERCLIP_BASE_URL     Optional. Default: http://localhost:3100/api
    MCP_AUTH_TOKEN         Required for HTTP transports. Bearer token MCP
                           clients must present; generate with
                           `openssl rand -hex 32`. Not used by stdio.
    MCP_PUBLIC_URL         Optional. Public HTTPS base URL of this server
                           (e.g. https://paperclip-mcp.example.com). When
                           set, enables full OAuth 2.0 support for Claude
                           web alongside the existing static bearer token.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import sys
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse

# ── Configuration ──────────────────────────────────────────────────────────────

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars can be set by the shell

BASE_URL: str = os.environ.get("PAPERCLIP_BASE_URL", "http://localhost:3100/api").rstrip("/")
API_KEY: str = os.environ.get("PAPERCLIP_API_KEY", "")
COMPANY: str = os.environ.get("PAPERCLIP_COMPANY_ID", "")
MCP_PUBLIC_URL: str = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] paperclip-mcp %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# ── HTTP client ────────────────────────────────────────────────────────────────

_HTTP_TIMEOUT = 30  # seconds

# Single pooled client, created in the server lifespan and closed on shutdown.
_http_client: httpx.AsyncClient | None = None


def _build_http_client() -> httpx.AsyncClient:
    """Build the shared Paperclip API client.

    follow_redirects is off so a redirect from the Paperclip server can never
    re-send the bearer token to another host. The Authorization header lives on
    the client and is never logged. Omit X-Paperclip-Run-Id — a fake UUID causes
    FK violations against heartbeat_runs when the API logs activity.
    """
    return httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        limits=httpx.Limits(max_connections=10),
        follow_redirects=False,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )


def _err(message: str, status: int | None = None) -> dict[str, Any]:
    """Return a structured error payload that signals isError to the MCP client."""
    payload: dict[str, Any] = {"isError": True, "message": message}
    if status is not None:
        payload["status"] = status
    return payload


# ── Parameter sanitization ─────────────────────────────────────────────────────
#
# Tool parameters are interpolated into URL paths. Every value MUST pass through
# one of the helpers below before reaching an f-string, otherwise a crafted id
# (e.g. "../companies/x" or "abc?admin=1") can rewrite the request path/query.

_MAX_PARAM_LEN = 128
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_ISSUE_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ISSUE_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
ISSUE_PRIORITIES = {"urgent", "high", "medium", "low"}
APPROVAL_STATUSES = {"pending", "approved", "rejected", "revision_requested"}


def _path_param(value: str, name: str = "parameter") -> str:
    """Validate and percent-encode a value destined for a URL path segment.

    Raises ValueError if the value is empty, too long, or contains control
    characters. The returned string is fully quoted (no characters are exempt),
    so path separators, "?", "#", etc. cannot alter the request target.
    """
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty.")
    if len(value) > _MAX_PARAM_LEN:
        raise ValueError(f"{name} is too long (max {_MAX_PARAM_LEN} characters).")
    if _CONTROL_CHARS_RE.search(value):
        raise ValueError(f"{name} contains control characters.")
    return urllib.parse.quote(value, safe="")


def _uuid_param(value: str, name: str) -> str:
    """Validate a parameter that is a UUID by contract; returns the canonical form."""
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a valid UUID.") from None


def _issue_ref(value: str, name: str = "issue_id") -> str:
    """Validate an issue reference: UUID or human-readable id like "CY-42"."""
    value = value.strip()
    if not _ISSUE_REF_RE.fullmatch(value):
        raise ValueError(
            f"{name} must be a UUID or an identifier like 'CY-42' "
            "(letters, digits, '-' and '_', 1-64 chars)."
        )
    return _path_param(value, name)


def _opt_uuid(value: str, name: str) -> str | None:
    """Validate an optional UUID-by-contract body parameter.

    Returns the canonical UUID string, or None when the value is blank (the
    field is simply omitted from the request body). Raises ValueError if a
    non-empty value is not a valid UUID — surfaced to the caller as a clear
    error instead of a downstream API rejection.
    """
    value = value.strip()
    if not value:
        return None
    return _uuid_param(value, name)


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    if _http_client is None:
        return _err("Server is not fully started yet (HTTP client unavailable). Retry shortly.")
    url = f"{BASE_URL}{path}"
    try:
        r = await _http_client.request(method, url, params=params, json=body)
        # 409 Conflict on checkout: another agent owns the issue — do not retry.
        if r.status_code == 409:
            return _err(
                "Conflict (409): resource is already checked out or owned by another agent. "
                "Do not retry this request.",
                status=409,
            )
        if r.status_code == 401:
            return _err(
                "Authentication failed (401): PAPERCLIP_API_KEY is invalid or expired. "
                "Generate a new key in Paperclip UI → Settings → API Keys.",
                status=401,
            )
        if r.status_code == 403:
            return _err(
                "Permission denied (403): this resource is outside the authorization boundary "
                "of the agent linked to PAPERCLIP_API_KEY. For broad orchestration access, "
                "generate an API key scoped to a company-admin/CEO agent — see README.",
                status=403,
            )
        if r.status_code == 404:
            return _err(
                "Not found (404): the requested resource does not exist. "
                "Check the ID you provided.",
                status=404,
            )
        # Redirects are not followed (see _build_http_client) and are unexpected.
        if r.is_redirect:
            return _err(
                f"Unexpected redirect ({r.status_code}) from Paperclip API — not followed.",
                status=r.status_code,
            )
        r.raise_for_status()
        # 204 No Content
        if r.status_code == 204 or not r.content:
            return {"ok": True}
        return r.json()
    except httpx.HTTPStatusError as exc:
        # Only status code and a truncated body — never request details/headers.
        return _err(
            f"HTTP {exc.response.status_code} from Paperclip API: {exc.response.text[:400]}",
            status=exc.response.status_code,
        )
    except httpx.RequestError as exc:
        # Report only the error class: exception text could embed request details.
        return _err(
            f"Could not reach Paperclip at {BASE_URL} ({type(exc).__name__}). "
            "Is the server running?"
        )


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return await _request("GET", path, params=params)


async def _post(path: str, body: dict[str, Any] | None = None) -> Any:
    return await _request("POST", path, body=body)


async def _patch(path: str, body: dict[str, Any]) -> Any:
    return await _request("PATCH", path, body=body)


# ── Compact projection ─────────────────────────────────────────────────────────
#
# All list_* tools apply a compact projection by default: only the most useful
# fields are returned. Pass full=True to get the raw API response.
# (200 issues raw ≈ 580 KB; compact ≈ 30 KB — ~20× smaller.)

_ISSUE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "identifier",
        "title",
        "status",
        "priority",
        "assigneeAgentId",
        "projectId",
        "goalId",
        "parentId",
        "labels",
        "updatedAt",
    }
)
_GOAL_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "status",
        "parentId",
        "level",
        "projectId",
        "updatedAt",
    }
)
_ACTIVITY_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "type",
        "agentId",
        "issueId",
        "goalId",
        "description",
        "createdAt",
    }
)
_AGENT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "role",
        "status",
        "model",
        "createdAt",
        "updatedAt",
    }
)
_APPROVAL_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "type",
        "status",
        "issueId",
        "goalId",
        "requestedByAgentId",
        "createdAt",
        "updatedAt",
    }
)
_PROJECT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "status",
        "goalId",
        "createdAt",
        "updatedAt",
    }
)
_COMMENT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "body",
        "authorAgentId",
        "createdAt",
        "updatedAt",
    }
)

# Envelope keys Paperclip may wrap list payloads in (checked in order).
_ENVELOPE_KEYS = (
    "data",
    "issues",
    "goals",
    "agents",
    "approvals",
    "projects",
    "comments",
    "activity",
    "results",
    "items",
)


def _compact(raw: Any, keys: frozenset[str]) -> Any:
    """Project each item in a list (or paginated envelope) to the given key set."""
    if isinstance(raw, dict) and raw.get("isError"):
        return raw

    def _proj(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: v for k, v in item.items() if k in keys}
        return item

    if isinstance(raw, list):
        return [_proj(i) for i in raw]
    if isinstance(raw, dict):
        for k in _ENVELOPE_KEYS:
            if k in raw and isinstance(raw[k], list):
                return {**raw, k: [_proj(i) for i in raw[k]]}
    return raw


# ── Startup validation ─────────────────────────────────────────────────────────


def _mask_key(key: str) -> str:
    """Render an API key safe for logs: only the last 4 characters survive."""
    return f"…{key[-4:]}" if len(key) >= 8 else "(set, too short to mask)"


def _is_private_host(host: str) -> bool:
    """True for localhost, .local names, and private/loopback IP literals."""
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _validate_config() -> None:
    """Fail fast with actionable error messages if required env vars are missing."""
    missing = [
        k
        for k, v in {
            "PAPERCLIP_API_KEY": API_KEY,
            "PAPERCLIP_COMPANY_ID": COMPANY,
        }.items()
        if not v
    ]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        log.error(
            "Copy .env.example to .env, fill in the values, then:\n"
            "  source .env && python -m paperclip_mcp\n"
            "Or set them in your shell before starting the server."
        )
        sys.exit(1)
    try:
        _uuid_param(COMPANY, "PAPERCLIP_COMPANY_ID")
    except ValueError:
        log.error(
            "PAPERCLIP_COMPANY_ID must be a UUID — find it in the Paperclip UI URL: "
            "/companies/{uuid}."
        )
        sys.exit(1)
    if not BASE_URL.startswith(("http://", "https://")):
        log.error("PAPERCLIP_BASE_URL must start with http:// or https:// (got: %s)", BASE_URL)
        sys.exit(1)
    host = urllib.parse.urlsplit(BASE_URL).hostname or ""
    if BASE_URL.startswith("http://") and not _is_private_host(host):
        log.warning(
            "PAPERCLIP_BASE_URL uses plain HTTP to a non-private host (%s) — "
            "the API key travels unencrypted. Switch to https://.",
            host,
        )


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    global _http_client
    _validate_config()
    _http_client = _build_http_client()
    log.info(
        "paperclip-mcp started — base: %s | company: %s | api key: %s (masked)",
        BASE_URL,
        COMPANY,
        _mask_key(API_KEY),
    )
    try:
        yield
    finally:
        await _http_client.aclose()
        _http_client = None
        log.info("paperclip-mcp stopped.")


# ── HTTP transport authentication ──────────────────────────────────────────────

OAUTH_CLIENT_ID = "paperclip-mcp-client"


class PaperclipAuthProvider(InMemoryOAuthProvider):  # type: ignore[misc]
    """Combined OAuth 2.0 + static bearer token auth provider.

    When MCP_PUBLIC_URL is set, use this instead of StaticBearerVerifier:
    - Claude.ai web: full OAuth authorization code flow
        client_id     = "paperclip-mcp-client"
        client_secret = MCP_AUTH_TOKEN
    - CLI / Claude Desktop: static bearer token (MCP_AUTH_TOKEN in the
        Authorization header) — unchanged, continues working.

    The static token is pre-loaded as a non-expiring access token in the
    InMemoryOAuthProvider's store, so both code paths share one validator.
    """

    def __init__(self, token: str, public_url: str) -> None:
        super().__init__(
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(enabled=False),
        )
        # Pre-register the OAuth client for Claude.ai web (secret = MCP_AUTH_TOKEN).
        self.clients[OAUTH_CLIENT_ID] = OAuthClientInformationFull(
            client_id=OAUTH_CLIENT_ID,
            client_secret=token,
            redirect_uris=[
                AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            ],
            grant_types=["authorization_code"],
            response_types=["code"],
            # Claude.ai sends client_secret as a POST form field (not Basic auth header)
            token_endpoint_auth_method="client_secret_post",
        )
        # Pre-load the static bearer token — expires_at=None means it never expires.
        self.access_tokens[token] = AccessToken(
            token=token,
            client_id="static-bearer",
            scopes=[],
            expires_at=None,
        )


class StaticBearerVerifier(TokenVerifier):  # type: ignore[misc]
    """FastMCP TokenVerifier backed by a single static secret (MCP_AUTH_TOKEN).

    Every HTTP request must carry `Authorization: Bearer <token>`; anything
    else gets a 401 from FastMCP's auth middleware before reaching MCP routes.
    Comparison uses secrets.compare_digest to resist timing attacks.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token.encode("utf-8"), self._token):
            return AccessToken(token=token, client_id="paperclip-mcp", scopes=[])
        return None


# ── MCP Server ─────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="paperclip",
    instructions=(
        "Manage a Paperclip AI agent orchestration platform. "
        "Use these tools to create and track issues (tasks), inspect agents, "
        "set goals, handle approvals, and monitor costs. "
        "All operations target a single Paperclip company configured via "
        "PAPERCLIP_COMPANY_ID."
    ),
    lifespan=_lifespan,
)


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(_request: Request) -> JSONResponse:
    """Unauthenticated liveness probe for container health checks.

    Custom routes sit outside FastMCP's auth middleware. Returns nothing but
    a constant payload — no configuration, versions, or state.
    """
    return JSONResponse({"ok": True})


# ── ISSUES ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_issues(
    status: str = "todo,in_progress",
    assignee_agent_id: str = "",
    project_id: str = "",
    goal_id: str = "",
    parent_issue_id: str = "",
    label: str = "",
    limit: int = 50,
    offset: int = 0,
    full: bool = False,
) -> Any:
    """List issues (tasks) in the active company.

    Returns a compact view by default (id, identifier, title, status, priority,
    assigneeAgentId, projectId, goalId, parentId, labels, updatedAt). Pass
    full=True to receive the complete raw API response.

    Args:
        status: Comma-separated issue statuses to include.
                Allowed values: todo, in_progress, blocked, done, cancelled.
                Default: "todo,in_progress"
        assignee_agent_id: UUID of the agent to filter by. Leave empty for all agents.
        project_id: UUID of the project to filter by. Leave empty for all projects.
        goal_id: UUID of the goal to filter by. Leave empty for all goals.
        parent_issue_id: UUID of the parent issue to list subtasks for. Leave empty for all.
        label: Label name to filter by. Leave empty to skip label filtering.
        limit: Maximum number of results to return (1–200). Default: 50.
        offset: Number of results to skip for pagination. Default: 0.
        full: If true, return the raw API response instead of the compact view.
    """
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    invalid = sorted(set(statuses) - ISSUE_STATUSES)
    if not statuses or invalid:
        return _err(
            f"Invalid status filter '{status}'. "
            f"Allowed (comma-separated): {', '.join(sorted(ISSUE_STATUSES))}."
        )
    params: dict[str, Any] = {
        "status": ",".join(statuses),
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
    }
    try:
        agent_uuid = _opt_uuid(assignee_agent_id, "assignee_agent_id")
        if agent_uuid is not None:
            params["assigneeAgentId"] = agent_uuid
        proj_uuid = _opt_uuid(project_id, "project_id")
        if proj_uuid is not None:
            params["projectId"] = proj_uuid
        goal_uuid = _opt_uuid(goal_id, "goal_id")
        if goal_uuid is not None:
            params["goalId"] = goal_uuid
        parent_uuid = _opt_uuid(parent_issue_id, "parent_issue_id")
        if parent_uuid is not None:
            params["parentIssueId"] = parent_uuid
    except ValueError as exc:
        return _err(str(exc))
    if label:
        params["label"] = label
    result = await _get(f"/companies/{COMPANY}/issues", params)
    return result if full else _compact(result, _ISSUE_KEYS)


@mcp.tool()
async def get_issue(issue_id: str) -> Any:
    """Get the full details of a single issue.

    Args:
        issue_id: Issue UUID or human-readable identifier (e.g. "CY-42").
    """
    try:
        ref = _issue_ref(issue_id)
    except ValueError as exc:
        return _err(str(exc))
    return await _get(f"/issues/{ref}")


@mcp.tool()
async def create_issue(
    title: str,
    description: str = "",
    assignee_agent_id: str = "",
    project_id: str = "",
    parent_issue_id: str = "",
    goal_id: str = "",
    priority: str = "medium",
    status: str = "",
    labels: str = "",
    work_mode: str = "",
) -> Any:
    """Create a new issue (task) and optionally assign it to an agent.

    Use this to delegate work to agents, create subtasks, or track action items.

    Args:
        title: Short, imperative task title (e.g. "Search cheese suppliers in Barcelona").
        description: Full instructions or context for the agent (Markdown supported).
        assignee_agent_id: UUID of the agent to assign. Leave empty to leave unassigned.
        project_id: UUID of the project this issue belongs to. Leave empty for no project.
        parent_issue_id: UUID of the parent issue when creating a subtask.
                         Leave empty for top-level.
        goal_id: UUID of the goal this issue should be linked to. Leave empty to inherit
                 the goal from the project (if any).
        priority: Task priority — urgent, high, medium, or low. Default: medium.
        status: Initial status — todo, in_progress, blocked, done, or cancelled.
                Leave empty for the API default (usually "todo").
        labels: Comma-separated label names to attach (e.g. "bug,backend"). Leave empty
                for no labels.
        work_mode: Optional work mode hint for the agent (e.g. "autonomous", "supervised").
                   Leave empty for the API default.
    """
    if priority not in ISSUE_PRIORITIES:
        return _err(
            f"Invalid priority '{priority}'. Allowed: {', '.join(sorted(ISSUE_PRIORITIES))}."
        )
    if status and status not in ISSUE_STATUSES:
        return _err(f"Invalid status '{status}'. Allowed: {', '.join(sorted(ISSUE_STATUSES))}.")
    body: dict[str, Any] = {"title": title, "priority": priority}
    if description:
        body["description"] = description
    if status:
        body["status"] = status
    if labels:
        body["labels"] = [lbl.strip() for lbl in labels.split(",") if lbl.strip()]
    if work_mode:
        body["workMode"] = work_mode
    try:
        for field, value in (
            ("assigneeAgentId", assignee_agent_id),
            ("projectId", project_id),
            ("parentIssueId", parent_issue_id),
            ("goalId", goal_id),
        ):
            canonical = _opt_uuid(value, field)
            if canonical is not None:
                body[field] = canonical
    except ValueError as exc:
        return _err(str(exc))
    return await _post(f"/companies/{COMPANY}/issues", body)


@mcp.tool()
async def update_issue(
    issue_id: str,
    title: str = "",
    description: str = "",
    status: str = "",
    assignee_agent_id: str = "",
    priority: str = "",
    project_id: str = "",
    parent_issue_id: str = "",
    goal_id: str = "",
    labels: str = "",
) -> Any:
    """Update an existing issue. Only fields you provide are changed.

    To unlink a relationship (goal, project, parent), pass the literal string
    "null" as the value (e.g. goal_id="null"). This sends JSON null to the API,
    clearing the link while leaving all other fields untouched.

    Args:
        issue_id: Issue UUID or identifier (e.g. "CY-42").
        title: New title. Leave empty to keep current value.
        description: New description (Markdown). Leave empty to keep current.
        status: New status — todo, in_progress, blocked, done, or cancelled.
                Leave empty to keep current.
        assignee_agent_id: New agent UUID. Leave empty to keep current assignee.
        priority: New priority — urgent, high, medium, or low. Leave empty to keep current.
        project_id: Move the issue to this project UUID, or "null" to remove from project.
        parent_issue_id: Re-parent the issue under this issue UUID, or "null" to make
                         it a top-level issue.
        goal_id: Link to this goal UUID, or "null" to unlink from current goal.
        labels: Comma-separated label names to set (replaces all existing labels).
                Leave empty to keep current labels.
    """
    try:
        ref = _issue_ref(issue_id)
    except ValueError as exc:
        return _err(str(exc))
    body: dict[str, Any] = {}
    if title:
        body["title"] = title
    if description:
        body["description"] = description
    if status:
        if status not in ISSUE_STATUSES:
            return _err(f"Invalid status '{status}'. Allowed: {', '.join(sorted(ISSUE_STATUSES))}.")
        body["status"] = status
    if assignee_agent_id:
        try:
            body["assigneeAgentId"] = _uuid_param(assignee_agent_id, "assignee_agent_id")
        except ValueError as exc:
            return _err(str(exc))
    if priority:
        if priority not in ISSUE_PRIORITIES:
            return _err(
                f"Invalid priority '{priority}'. Allowed: {', '.join(sorted(ISSUE_PRIORITIES))}."
            )
        body["priority"] = priority
    if labels:
        body["labels"] = [lbl.strip() for lbl in labels.split(",") if lbl.strip()]
    try:
        for field, value in (
            ("projectId", project_id),
            ("parentIssueId", parent_issue_id),
            ("goalId", goal_id),
        ):
            if value == "null":
                body[field] = None
            else:
                canonical = _opt_uuid(value, field)
                if canonical is not None:
                    body[field] = canonical
    except ValueError as exc:
        return _err(str(exc))
    if not body:
        return _err(
            "No fields to update. Provide at least one of: title, description, status, "
            "assignee_agent_id, priority, project_id, parent_issue_id, goal_id, labels."
        )
    return await _patch(f"/issues/{ref}", body)


@mcp.tool()
async def checkout_issue(issue_id: str) -> Any:
    """Atomically assign an issue to the current agent and mark it in_progress.

    A 409 Conflict response means another agent already owns this issue — do NOT retry.
    Use release_issue to undo a checkout.

    Args:
        issue_id: Issue UUID or identifier to check out.
    """
    try:
        ref = _issue_ref(issue_id)
    except ValueError as exc:
        return _err(str(exc))
    return await _post(f"/issues/{ref}/checkout")


@mcp.tool()
async def release_issue(issue_id: str) -> Any:
    """Release an issue: unassign it and revert it to its previous state.

    This is the inverse of checkout_issue. Use when an agent cannot complete a task
    and it should be returned to the queue.

    Args:
        issue_id: Issue UUID or identifier to release.
    """
    try:
        ref = _issue_ref(issue_id)
    except ValueError as exc:
        return _err(str(exc))
    return await _post(f"/issues/{ref}/release")


@mcp.tool()
async def comment_on_issue(
    issue_id: str,
    body: str,
    reopen: bool = False,
) -> Any:
    """Add a comment to an issue (supports Markdown).

    Args:
        issue_id: Issue UUID or identifier.
        body: Comment text. Markdown is supported.
        reopen: Set to true to reopen the issue when posting this comment.
                Only effective if the issue is currently closed.
    """
    try:
        ref = _issue_ref(issue_id)
    except ValueError as exc:
        return _err(str(exc))
    payload: dict[str, Any] = {"body": body}
    if reopen:
        payload["reopen"] = True
    return await _post(f"/issues/{ref}/comments", payload)


@mcp.tool()
async def list_comments(issue_id: str, full: bool = False) -> Any:
    """List all comments on an issue, in chronological order.

    Returns a compact view by default (id, body, authorAgentId, createdAt,
    updatedAt). Pass full=True for the raw API response.

    Args:
        issue_id: Issue UUID or human-readable identifier (e.g. "CY-42").
        full: If true, return the raw API response instead of the compact view.
    """
    try:
        ref = _issue_ref(issue_id)
    except ValueError as exc:
        return _err(str(exc))
    result = await _get(f"/issues/{ref}/comments")
    return result if full else _compact(result, _COMMENT_KEYS)


# ── AGENTS ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_agents(full: bool = False) -> Any:
    """List all agents in the active company.

    Returns a compact view by default (id, name, role, status, model,
    createdAt, updatedAt). Pass full=True to include system prompts,
    configuration, and all other fields.

    Args:
        full: If true, return the raw API response instead of the compact view.
    """
    result = await _get(f"/companies/{COMPANY}/agents")
    return result if full else _compact(result, _AGENT_KEYS)


@mcp.tool()
async def get_agent(agent_id: str = "me") -> Any:
    """Get details for a specific agent, or the currently authenticated agent.

    Args:
        agent_id: Agent UUID, or the literal string "me" to get the current agent identity.
                  Default: "me"
    """
    if agent_id.strip().lower() == "me":
        return await _get("/agents/me")
    try:
        ref = _uuid_param(agent_id, "agent_id")
    except ValueError as exc:
        return _err(str(exc))
    return await _get(f"/agents/{ref}")


@mcp.tool()
async def invoke_agent_heartbeat(agent_id: str) -> Any:
    """Manually trigger an immediate heartbeat (work cycle) for an agent.

    Use this to wake an idle agent, force it to pick up new assignments,
    or run it outside its normal schedule.

    Args:
        agent_id: UUID of the agent to trigger.
    """
    try:
        ref = _uuid_param(agent_id, "agent_id")
    except ValueError as exc:
        return _err(str(exc))
    return await _post(f"/agents/{ref}/heartbeat/invoke")


# ── GOALS ──────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_goals(
    limit: int = 50,
    offset: int = 0,
    full: bool = False,
) -> Any:
    """List all strategic goals for the active company.

    Returns a compact view by default (id, title, status, parentId, level,
    projectId, updatedAt). Pass full=True for the raw API response.

    Args:
        limit: Maximum number of results to return (1–200). Default: 50.
        offset: Number of results to skip for pagination. Default: 0.
        full: If true, return the raw API response instead of the compact view.
    """
    params: dict[str, Any] = {
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
    }
    result = await _get(f"/companies/{COMPANY}/goals", params)
    return result if full else _compact(result, _GOAL_KEYS)


@mcp.tool()
async def get_goal(goal_id: str) -> Any:
    """Get the full details of a single goal.

    Args:
        goal_id: Goal UUID.
    """
    try:
        ref = _uuid_param(goal_id, "goal_id")
    except ValueError as exc:
        return _err(str(exc))
    return await _get(f"/goals/{ref}")


@mcp.tool()
async def create_goal(
    title: str,
    description: str = "",
    parent_id: str = "",
    level: str = "",
    project_id: str = "",
) -> Any:
    """Create a new strategic goal for the active company.

    Goals provide high-level direction to agents. They appear in agent context
    so agents can align their work accordingly. Goals can be nested: pass
    parent_id to create a sub-goal under an existing goal, so work traces back
    up to the parent objective.

    Args:
        title: Goal title (e.g. "Reach 300 packs/month in sales by June 2026").
        description: Extended context, success criteria, and constraints (Markdown supported).
        parent_id: UUID of the parent goal to nest this goal under. Leave empty for top-level.
        level: Optional hierarchy level for this goal (values defined by the Paperclip
               API, e.g. company/objective/project/task). Leave empty for the API default.
        project_id: UUID of the project to attach this goal to. Leave empty for none.
    """
    body: dict[str, Any] = {"title": title}
    if description:
        body["description"] = description
    if level.strip():
        body["level"] = level.strip()
    try:
        parent = _opt_uuid(parent_id, "parent_id")
        if parent is not None:
            body["parentId"] = parent
        project = _opt_uuid(project_id, "project_id")
        if project is not None:
            body["projectId"] = project
    except ValueError as exc:
        return _err(str(exc))
    return await _post(f"/companies/{COMPANY}/goals", body)


@mcp.tool()
async def update_goal(
    goal_id: str,
    title: str = "",
    description: str = "",
    status: str = "",
    parent_id: str = "",
    level: str = "",
) -> Any:
    """Update an existing goal's title, description, status, parent, or level.

    Pass parent_id to nest this goal under another goal (e.g. attach a project
    goal under the company mission) so results roll up the hierarchy. Pass
    parent_id="null" to detach from the current parent and make it a top-level
    goal. Only the fields you provide are changed.

    Args:
        goal_id: Goal UUID.
        title: New title. Leave empty to keep current.
        description: New description. Leave empty to keep current.
        status: New goal status. Leave empty to keep current.
        parent_id: UUID of the new parent goal, or "null" to unlink from current parent.
                   Leave empty to keep the current parent.
        level: New hierarchy level (values defined by the Paperclip API,
               e.g. company/objective/project/task). Leave empty to keep current.
    """
    try:
        ref = _uuid_param(goal_id, "goal_id")
    except ValueError as exc:
        return _err(str(exc))
    body: dict[str, Any] = {}
    if title:
        body["title"] = title
    if description:
        body["description"] = description
    if status:
        body["status"] = status
    if level.strip():
        body["level"] = level.strip()
    if parent_id == "null":
        body["parentId"] = None
    elif parent_id:
        try:
            body["parentId"] = _uuid_param(parent_id, "parent_id")
        except ValueError as exc:
            return _err(str(exc))
    if not body:
        return _err(
            "No fields to update. Provide at least one of: "
            "title, description, status, parent_id, level."
        )
    return await _patch(f"/goals/{ref}", body)


# ── PROJECTS ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_projects(full: bool = False) -> Any:
    """List all projects in the active company.

    Returns a compact view by default (id, name, status, goalId, createdAt,
    updatedAt). Pass full=True for the raw API response.

    Args:
        full: If true, return the raw API response instead of the compact view.
    """
    result = await _get(f"/companies/{COMPANY}/projects")
    return result if full else _compact(result, _PROJECT_KEYS)


@mcp.tool()
async def get_project(project_id: str) -> Any:
    """Get the full details of a single project.

    Args:
        project_id: Project UUID.
    """
    try:
        ref = _uuid_param(project_id, "project_id")
    except ValueError as exc:
        return _err(str(exc))
    return await _get(f"/projects/{ref}")


# ── APPROVALS ──────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_approvals(status: str = "pending", full: bool = False) -> Any:
    """List approval requests in the active company.

    Returns a compact view by default (id, type, status, issueId, goalId,
    requestedByAgentId, createdAt, updatedAt). Pass full=True for the raw
    API response.

    Args:
        status: Filter by status. Allowed values:
                pending, approved, rejected, revision_requested.
                Default: "pending"
        full: If true, return the raw API response instead of the compact view.
    """
    if status not in APPROVAL_STATUSES:
        return _err(f"Invalid status '{status}'. Allowed: {', '.join(sorted(APPROVAL_STATUSES))}.")
    result = await _get(f"/companies/{COMPANY}/approvals", {"status": status})
    return result if full else _compact(result, _APPROVAL_KEYS)


@mcp.tool()
async def approve(approval_id: str, comment: str = "") -> Any:
    """Approve a pending approval request.

    Args:
        approval_id: Approval UUID.
        comment: Optional approval note to attach (e.g. conditions, context).
    """
    try:
        ref = _uuid_param(approval_id, "approval_id")
    except ValueError as exc:
        return _err(str(exc))
    body: dict[str, Any] = {}
    if comment:
        body["comment"] = comment
    return await _post(f"/approvals/{ref}/approve", body)


@mcp.tool()
async def reject(approval_id: str, comment: str = "") -> Any:
    """Reject a pending approval request.

    Args:
        approval_id: Approval UUID.
        comment: Reason for rejection — strongly recommended so the agent understands why.
    """
    try:
        ref = _uuid_param(approval_id, "approval_id")
    except ValueError as exc:
        return _err(str(exc))
    body: dict[str, Any] = {}
    if comment:
        body["comment"] = comment
    return await _post(f"/approvals/{ref}/reject", body)


@mcp.tool()
async def request_approval_revision(approval_id: str, comment: str) -> Any:
    """Request a revision on a pending approval without fully rejecting it.

    The submitting agent will receive the comment and can resubmit.

    Args:
        approval_id: Approval UUID.
        comment: Required. Specific feedback describing what must change before approval.
    """
    try:
        ref = _uuid_param(approval_id, "approval_id")
    except ValueError as exc:
        return _err(str(exc))
    if not comment.strip():
        return _err("A comment is required when requesting a revision.")
    return await _post(f"/approvals/{ref}/request-revision", {"comment": comment})


# ── COSTS & MONITORING ─────────────────────────────────────────────────────────


@mcp.tool()
async def get_cost_summary() -> Any:
    """Get aggregate token usage and spend for the active company this billing period.

    Returns total spend, remaining budget, and a per-agent cost breakdown.
    Use this to monitor AI spend and detect runaway agents.
    """
    return await _get(f"/companies/{COMPANY}/costs/summary")


@mcp.tool()
async def get_dashboard() -> Any:
    """Get a high-level health summary for the active company.

    Returns: agent count, open/in-progress/blocked issue counts, stale tasks,
    recent activity digest, and current-period cost totals.
    """
    return await _get(f"/companies/{COMPANY}/dashboard")


@mcp.tool()
async def list_activity(
    agent_id: str = "",
    limit: int = 20,
    offset: int = 0,
    full: bool = False,
) -> Any:
    """Retrieve the audit trail of recent actions in the active company.

    Returns a compact view by default (id, type, agentId, issueId, goalId,
    description, createdAt). Pass full=True for the raw API response.

    Args:
        agent_id: Filter to a specific agent UUID. Leave empty for all agents.
        limit: Maximum number of entries to return (1–100). Default: 20.
        offset: Number of entries to skip for pagination. Default: 0.
        full: If true, return the raw API response instead of the compact view.
    """
    params: dict[str, Any] = {
        "limit": max(1, min(limit, 100)),
        "offset": max(0, offset),
    }
    if agent_id:
        try:
            params["agentId"] = _uuid_param(agent_id, "agent_id")
        except ValueError as exc:
            return _err(str(exc))
    result = await _get(f"/companies/{COMPANY}/activity", params)
    return result if full else _compact(result, _ACTIVITY_KEYS)


# ── ENTRY POINT ────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point — invoked via `paperclip-mcp` or `python -m paperclip_mcp`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="paperclip-mcp",
        description="MCP server for the Paperclip AI agent orchestration platform.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Use 0.0.0.0 only in trusted local networks.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9011,
        help="Bind port.",
    )
    parser.add_argument(
        "--transport",
        default="streamable-http",
        choices=["streamable-http", "sse", "stdio"],
        help=(
            "MCP transport protocol. "
            "'streamable-http' for Claude Code / mcp-proxy; "
            "'stdio' for Claude Desktop."
        ),
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        # stdio is only reachable by the local parent process — no token needed.
        mcp.run(transport="stdio")
        return

    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not token:
        log.error(
            "MCP_AUTH_TOKEN is not set — refusing to start an unauthenticated "
            "HTTP transport (it would expose your Paperclip API key to anyone "
            "who can reach port %d).\n"
            "  Generate a token:  openssl rand -hex 32\n"
            "  Then set it:       export MCP_AUTH_TOKEN=<token>  (or add to .env)\n"
            "  No-network option: paperclip-mcp --transport stdio",
            args.port,
        )
        sys.exit(1)
    if MCP_PUBLIC_URL:
        # OAuth 2.0 (Claude.ai web) + static bearer (CLI/Desktop) — both active.
        log.info("OAuth enabled — client_id: %s | public_url: %s", OAUTH_CLIENT_ID, MCP_PUBLIC_URL)
        mcp.auth = PaperclipAuthProvider(token, MCP_PUBLIC_URL)
    else:
        # Static bearer only — no OAuth discovery, simpler for LAN-only setups.
        mcp.auth = StaticBearerVerifier(token)
    mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
