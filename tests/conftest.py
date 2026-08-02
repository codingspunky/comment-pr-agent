"""Test environment.

The webhook module now refuses to import without a real webhook secret (an
empty one used to mean "HMAC with an empty key", i.e. authenticate anybody).
Set the environment before any agent module is imported.
"""
from __future__ import annotations

import os

os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret-not-a-placeholder")
os.environ.setdefault("GROQ_API_KEY", "gsk_test_key_not_real")
os.environ.setdefault("GITHUB_APP_ID", "99999")
os.environ.setdefault("AGENT_MAX_TOKENS", "8000")
