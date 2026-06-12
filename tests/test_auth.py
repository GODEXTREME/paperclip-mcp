"""Tests for HTTP bearer-token authentication and fail-closed startup."""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from paperclip_mcp import server
from paperclip_mcp.server import StaticBearerVerifier

TOKEN = "test-token-abc123"

INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "tests", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@asynccontextmanager
async def auth_client() -> AsyncIterator[httpx.AsyncClient]:
    """ASGI test client against the real FastMCP HTTP app with auth enabled.

    Used as a context manager inside each test (not a fixture) so the app
    lifespan enters and exits in the same asyncio task — anyio cancel scopes
    require it.
    """
    previous_auth = server.mcp.auth
    server.mcp.auth = StaticBearerVerifier(TOKEN)
    try:
        app = server.mcp.http_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        server.mcp.auth = previous_auth


async def test_request_without_token_is_401() -> None:
    async with auth_client() as client:
        r = await client.post("/mcp", json=INIT_PAYLOAD, headers=MCP_HEADERS)
        assert r.status_code == 401


async def test_request_with_wrong_token_is_401() -> None:
    async with auth_client() as client:
        r = await client.post(
            "/mcp",
            json=INIT_PAYLOAD,
            headers={**MCP_HEADERS, "Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401


async def test_401_does_not_leak_details() -> None:
    async with auth_client() as client:
        r = await client.post("/mcp", json=INIT_PAYLOAD, headers=MCP_HEADERS)
        body = r.text.lower()
        assert TOKEN not in r.text
        assert "test-api-key" not in body
        assert "traceback" not in body


async def test_request_with_correct_token_succeeds() -> None:
    async with auth_client() as client:
        r = await client.post(
            "/mcp",
            json=INIT_PAYLOAD,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code == 200


async def test_healthz_needs_no_token() -> None:
    async with auth_client() as client:
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


def test_http_transport_refuses_to_start_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["paperclip-mcp"])  # default: streamable-http
    with pytest.raises(SystemExit) as exc_info:
        server.main()
    assert exc_info.value.code == 1


def test_http_transport_refuses_blank_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "   ")
    monkeypatch.setattr(sys, "argv", ["paperclip-mcp"])
    with pytest.raises(SystemExit):
        server.main()


def test_stdio_transport_needs_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["paperclip-mcp", "--transport", "stdio"])
    called: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> None:
        called.update(kwargs)

    monkeypatch.setattr(server.mcp, "run", fake_run)
    server.main()
    assert called == {"transport": "stdio"}


async def test_verifier_accepts_only_exact_token() -> None:
    verifier = StaticBearerVerifier(TOKEN)
    assert await verifier.verify_token(TOKEN) is not None
    assert await verifier.verify_token(TOKEN + "x") is None
    assert await verifier.verify_token("") is None
