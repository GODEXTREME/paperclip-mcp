"""Shared test configuration.

The server module reads its configuration from the environment at import
time, so the variables must be in place before any test imports it.
"""

import os

os.environ.setdefault("PAPERCLIP_API_KEY", "test-api-key-0123456789abcdef")
os.environ.setdefault("PAPERCLIP_COMPANY_ID", "00000000-0000-4000-8000-000000000000")
os.environ.setdefault("PAPERCLIP_BASE_URL", "http://localhost:3100/api")
