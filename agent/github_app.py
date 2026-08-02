"""GitHub authentication.

Two supported modes:

  1. A pre-minted token in GITHUB_TOKEN. This is what GitHub Actions injects;
     it is scoped to the one repository and expires with the job. Preferred,
     because it means no long-lived private key sits on the machine that runs
     repo-supplied validation commands.
  2. A GitHub App private key -> short-lived RS256 JWT -> installation access
     token, cached until shortly before expiry. Needed for the webhook service.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Tuple

import jwt
import requests

from .config import Settings, get_settings


def mint_app_jwt(settings: Settings | None = None) -> str:
    """Create a ~9-minute RS256 JWT signed with the App private key."""
    settings = settings or get_settings()
    settings.require("github_app_id", "github_app_private_key")
    now = int(time.time())
    payload = {
        "iat": now - 60,            # backdate to tolerate clock skew
        "exp": now + 9 * 60,        # GitHub max is 10 minutes
        "iss": str(settings.github_app_id),
    }
    return jwt.encode(payload, settings.github_app_private_key, algorithm="RS256")


_token_cache: Dict[int, Tuple[str, float]] = {}
_cache_lock = Lock()


def installation_token(installation_id: int, settings: Settings | None = None) -> str:
    """Return a valid installation access token, minting/caching as needed."""
    settings = settings or get_settings()
    now = time.time()

    with _cache_lock:
        cached = _token_cache.get(installation_id)
        if cached and cached[1] - 60 > now:  # 60s safety margin
            return cached[0]

    app_jwt = mint_app_jwt(settings)
    resp = requests.post(
        f"{settings.github_api_base}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["token"]
    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    expiry_epoch = expires_at.replace(tzinfo=timezone.utc).timestamp()

    with _cache_lock:
        _token_cache[installation_id] = (token, expiry_epoch)
    return token


def resolve_token(installation_id: Optional[int], settings: Settings | None = None) -> str:
    """Prefer an injected token; fall back to App auth."""
    settings = settings or get_settings()
    if settings.github_token:
        return settings.github_token
    if installation_id is None:
        raise RuntimeError(
            "no GitHub credentials: set GITHUB_TOKEN, or supply an installation id "
            "together with GITHUB_APP_ID and a private key")
    return installation_token(int(installation_id), settings)
