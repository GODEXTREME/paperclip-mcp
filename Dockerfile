# syntax=docker/dockerfile:1

# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install pinned dependencies first so the layer is cached across code changes.
COPY requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install --no-deps .

# ── Runtime ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

RUN useradd --uid 1000 --user-group --create-home appuser

COPY --from=builder /install /usr/local

USER appuser

EXPOSE 9011

# /healthz is served outside the MCP auth middleware and returns only
# {"ok": true} — no token required, nothing sensitive exposed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9011/healthz', timeout=3)"]

# 0.0.0.0 is correct inside the container; actual exposure is decided by the
# compose port mapping (see docker-compose.yml).
ENTRYPOINT ["paperclip-mcp", "--host", "0.0.0.0", "--port", "9011"]
