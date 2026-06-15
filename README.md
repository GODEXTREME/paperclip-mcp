# paperclip-mcp

MCP server for the [Paperclip](https://github.com/paperclipai/paperclip) AI agent orchestration platform.

Exposes Paperclip's REST API as [Model Context Protocol](https://modelcontextprotocol.io) tools, so any MCP-compatible AI assistant (Claude, etc.) can manage issues, agents, goals, approvals, and costs through natural language.

---

## Features

| Category | Tools |
|---|---|
| **Issues** | `list_issues` · `get_issue` · `create_issue` · `update_issue` · `checkout_issue` · `release_issue` · `comment_on_issue` |
| **Agents** | `list_agents` · `get_agent` · `invoke_agent_heartbeat` |
| **Goals** | `list_goals` · `create_goal` · `update_goal` |
| **Approvals** | `list_approvals` · `approve` · `reject` · `request_approval_revision` |
| **Monitoring** | `get_cost_summary` · `get_dashboard` · `list_activity` |

### Hierarchy & linkage

Goals and issues can be organized into a hierarchy so work traces back to the mission:

- `create_goal` / `update_goal` accept **`parent_id`** (nest a goal under another goal) and
  **`level`**. `create_goal` also accepts **`project_id`**.
- `create_issue` / `update_issue` accept **`goal_id`**, **`project_id`** and
  **`parent_issue_id`** — link an issue to a specific goal, move it between projects, or
  re-parent it. (`create_issue` already validates `priority`.)

All id parameters are validated as UUIDs before the request is sent.

---

## Requirements

- Python 3.10+
- A running [Paperclip](https://github.com/paperclipai/paperclip) instance
- An Agent API key (generated in Paperclip UI → Settings → API Keys)

---

## Installation

### Option A — pip / uv (recommended)

```bash
# Clone the repo
git clone https://github.com/wizarck/paperclip-mcp
cd paperclip-mcp

# Install (editable for local use, or drop -e for production)
pip install -e .
# or
uv pip install -e .
```

### Option B — Run directly without installing

```bash
pip install fastmcp httpx python-dotenv
python src/paperclip_mcp/server.py
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```dotenv
PAPERCLIP_BASE_URL=http://localhost:3100/api   # default, change if needed
PAPERCLIP_API_KEY=your_api_key_here
PAPERCLIP_COMPANY_ID=your_company_uuid_here
MCP_AUTH_TOKEN=your_generated_token_here       # required for HTTP transports
```

> **Security**: Never commit `.env` to version control. It is listed in `.gitignore`.

**Where to find these values:**
- `PAPERCLIP_API_KEY` — Paperclip UI → Settings → API Keys → New Key
- `PAPERCLIP_COMPANY_ID` — visible in the URL when viewing your company: `/companies/{uuid}`
- `MCP_AUTH_TOKEN` — generate yourself: `openssl rand -hex 32`. Every HTTP request to the
  MCP server must present it as `Authorization: Bearer <token>`. The server **refuses to
  start** over HTTP without it (stdio does not need it).

---

## Usage

### Start the server

```bash
# HTTP (for Claude Code / mcp-proxy) — default port 9011
paperclip-mcp

# Custom port
paperclip-mcp --port 9012

# stdio transport (for Claude Desktop)
paperclip-mcp --transport stdio

# All options
paperclip-mcp --help
```

### Register with Claude Code

```bash
# HTTP transport (persistent — survives Claude restarts)
claude mcp add paperclip --transport http http://localhost:9011/mcp \
  --header "Authorization: Bearer YOUR_MCP_AUTH_TOKEN"

# stdio transport (Claude Desktop — add to claude_desktop_config.json)
```

#### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "paperclip": {
      "command": "paperclip-mcp",
      "args": ["--transport", "stdio"],
      "env": {
        "PAPERCLIP_API_KEY": "your_api_key",
        "PAPERCLIP_COMPANY_ID": "your_company_uuid"
      }
    }
  }
}
```

---

## Example interactions

Once registered, you can ask your AI assistant:

```
"What tasks does the Purchasing agent have open?"
→ calls list_issues(assignee_agent_id="...", status="todo,in_progress")

"Create a task for the CEO agent to search for new cheese suppliers in Barcelona"
→ calls create_issue(title="Search cheese suppliers in Barcelona", assignee_agent_id="...")

"Approve the pending hire request"
→ calls list_approvals(status="pending") + approve(approval_id="...")

"How much have we spent on tokens this month, broken down by agent?"
→ calls get_cost_summary()

"Wake up the Administration agent now"
→ calls invoke_agent_heartbeat(agent_id="...")
```

---

## Auto-start with the MCP stack

Add to your stack startup script:

```bash
# Check if already running (uses the unauthenticated /healthz probe)
curl -s --max-time 1 http://localhost:9011/healthz > /dev/null 2>&1 || \
  nohup paperclip-mcp > /tmp/paperclip-mcp.log 2>&1 &
```

---

## Deploy no Portainer (NAS)

Run the server as a hardened container on your NAS and reach it from Claude on
another machine of your network.

**1. Create the Stack**

In Portainer: **Stacks → Add stack**, name it `paperclip-mcp`, and paste the
contents of [`docker-compose.yml`](docker-compose.yml) (or point the stack at
this repository).

**2. Define the environment variables**

In the stack's **Environment variables** panel, add the four variables — mark
the two secrets as hidden/sensitive:

| Variable | Secret | Value |
|---|---|---|
| `PAPERCLIP_API_KEY` | ✅ | From Paperclip UI → Settings → API Keys |
| `PAPERCLIP_COMPANY_ID` | — | Company UUID from the Paperclip UI URL |
| `PAPERCLIP_BASE_URL` | — | e.g. `http://192.168.1.10:3100/api` |
| `MCP_AUTH_TOKEN` | ✅ | Generate with `openssl rand -hex 32` |

**3. Choose the port mapping**

The compose file ships with `127.0.0.1:9011:9011` (recommended): the server is
only reachable from the NAS itself, and you front it with a TLS reverse proxy
or Tailscale running on the NAS. To expose it directly on the LAN instead,
switch to the commented `9011:9011` mapping — acceptable only because every
request now requires the bearer token, but TLS via reverse proxy is still
recommended (the token travels in cleartext over plain HTTP).

**4. Deploy and verify**

Deploy the stack, then from the NAS:

```bash
curl http://127.0.0.1:9011/healthz        # → {"ok":true}
curl -i http://127.0.0.1:9011/mcp         # → 401 (auth is enforced)
```

**5. Register with Claude Code (on your other machine)**

```bash
claude mcp add paperclip --transport http http://IP_DO_NAS:9011/mcp \
  --header "Authorization: Bearer SEU_MCP_AUTH_TOKEN"
```

> **Claude Desktop**: it speaks stdio, so use an MCP HTTP proxy such as
> [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) in
> `claude_desktop_config.json`, passing the same
> `Authorization: Bearer <token>` header to it.

---

## Security

- **Bearer-token auth (fail-closed)** — HTTP transports refuse to start without
  `MCP_AUTH_TOKEN`; requests without the exact token get `401`. Token comparison
  uses `secrets.compare_digest` (timing-attack resistant). stdio needs no token.
- **No destructive tools** — `delete_issue` was removed; no tool issues HTTP
  `DELETE` requests.
- **Input sanitization** — every tool parameter that reaches a URL path is
  validated (UUID or strict identifier pattern) and percent-encoded, blocking
  path/query injection (`../`, `?`, `#`, `/`).
- **Hardened HTTP client** — single pooled `httpx` client with
  `follow_redirects=False` (a redirect can never re-send your API key to
  another host), 30 s timeout, 10-connection limit.
- **Secret hygiene** — the Paperclip API key is never logged (startup shows only
  the last 4 characters); errors returned to MCP clients never include request
  headers.
- **Container hardening** — non-root user (UID 1000), `read_only` filesystem,
  `cap_drop: ALL`, `no-new-privileges`, unauthenticated `/healthz` that exposes
  nothing but `{"ok": true}`.
- **Pinned dependencies** — `requirements.lock` drives the Docker build;
  audited with `pip-audit`.

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Lint
ruff check src/
ruff format src/

# Type check
mypy src/

# Tests
pytest

# Dependency audit
pip-audit -r requirements.lock --disable-pip --no-deps
```

---

## Architecture notes

- **Who should use this MCP**: Human operators managing agents via Claude Code or Claude Desktop.
- **Do agents need this MCP?**: No — Paperclip agents already interact with the REST API directly via HTTP in their HEARTBEAT protocol. This MCP is for the human operator layer.
- **Hermes agents**: If you switch to [Hermes](https://github.com/NousResearch/hermes-paperclip-adapter), this MCP is automatically available since Hermes supports MCP natively.
- **Transport choice**: Use `streamable-http` for Claude Code and mcp-proxy integrations. Use `stdio` for Claude Desktop.
- **Security**: The server binds to `127.0.0.1` by default. HTTP transports always require the `MCP_AUTH_TOKEN` bearer token (see [Security](#security)); even so, prefer keeping it behind a reverse proxy with TLS when exposed beyond localhost — it carries your Paperclip API key.

---

## License

MIT — see [LICENSE](LICENSE).
