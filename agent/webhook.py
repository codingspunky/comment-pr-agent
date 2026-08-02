"""FastAPI webhook receiver.

Responsibilities (all fast, on the request path):
  - verify the HMAC signature
  - filter to relevant events + trigger phrase
  - authorize the comment author
  - enqueue the job and return 200 immediately

The agent run happens off the request path. Here it uses a FastAPI background
task; in production swap `enqueue` for SQS/Celery so runs survive restarts.
"""
from __future__ import annotations

import logging
from collections import deque

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .commands import is_addressed, is_authorized, load_allowlist_from_env, verify_signature
from .config import get_settings
from .runner import run_agent

log = logging.getLogger("comment-pr-agent")
app = FastAPI(title="comment-pr-agent")
SETTINGS = get_settings()

# Fail closed at import. An empty or placeholder GITHUB_WEBHOOK_SECRET used to
# start fine and then HMAC every request with an empty key -- which anyone can
# reproduce, so the endpoint authenticated the world. Refusing to boot is the
# only safe behaviour.
SETTINGS.require("github_webhook_secret")

RELEVANT_EVENTS = {"issue_comment", "pull_request_review_comment"}

# GitHub redelivers webhooks (retries, manual redelivery). Per-process dedupe
# on X-GitHub-Delivery prevents duplicate agent runs -> duplicate commits/PRs.
# NOTE: in-memory, so multi-replica deployments need queue-level dedupe (SQS
# FIFO / idempotency key) -- this covers the single-instance + restart-races case.
_SEEN_MAX = 2048
_seen_deliveries: set[str] = set()
_seen_order: deque[str] = deque()


def _is_duplicate_delivery(delivery_id: str) -> bool:
    """Check-and-record. Only called once we are about to actually enqueue, so
    a delivery rejected for signature/auth/filtering does not burn its id and
    block a legitimate redelivery."""
    if not delivery_id:
        return False
    if delivery_id in _seen_deliveries:
        return True
    _seen_deliveries.add(delivery_id)
    _seen_order.append(delivery_id)
    if len(_seen_order) > _SEEN_MAX:
        _seen_deliveries.discard(_seen_order.popleft())
    return False


@app.get("/healthz")
async def healthz():
    """Async on purpose: a sync handler runs on the same bounded threadpool as
    the background agent runs, so a few long validations would make the health
    check hang and get the container restarted mid-run."""
    return {"status": "ok"}


def _extract(event: str, payload: dict) -> dict | None:
    """Normalise the two event shapes into a single job dict, or None to ignore."""
    if payload.get("action") != "created":
        return None

    comment = payload.get("comment", {}) or {}
    sender = payload.get("sender", {}) or {}
    installation = payload.get("installation", {}) or {}
    repo = payload.get("repository", {}) or {}

    # Ignore anything authored by a bot (loop guard).
    if sender.get("type") == "Bot":
        return None

    job = {
        "repo": repo.get("full_name"),
        "installation_id": installation.get("id"),
        "comment_body": comment.get("body", ""),
        "comment_author": sender.get("login"),
        "author_association": comment.get("author_association"),
    }

    if event == "issue_comment":
        issue = payload.get("issue", {}) or {}
        job["issue_number"] = issue.get("number")
        # `pull_request` key is present iff the issue is actually a PR.
        job["pr_number"] = issue.get("number") if issue.get("pull_request") else None
    else:  # pull_request_review_comment
        pr = payload.get("pull_request", {}) or {}
        job["issue_number"] = pr.get("number")
        job["pr_number"] = pr.get("number")

    return job


@app.post("/webhook")
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
):
    body = await request.body()

    if not verify_signature(SETTINGS.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        return {"ok": True}
    if x_github_event not in RELEVANT_EVENTS:
        return {"ignored": "event"}

    payload = await request.json()
    job = _extract(x_github_event, payload)
    if not job:
        return {"ignored": "not actionable"}

    if not is_addressed(job["comment_body"], SETTINGS.trigger_phrase, SETTINGS.require_trigger):
        return {"ignored": "not addressed to agent"}

    if not is_authorized(
        job["author_association"], job["comment_author"], load_allowlist_from_env()):
        log.warning("unauthorized agent attempt by %s on %s", job["comment_author"], job["repo"])
        return {"ignored": "unauthorized author"}

    if not job.get("repo") or job.get("issue_number") is None:
        raise HTTPException(status_code=400, detail="missing repo or issue number")
    if not job.get("installation_id") and not SETTINGS.github_token:
        raise HTTPException(status_code=400, detail="missing installation id")

    # Recorded last, so only an accepted delivery consumes its id.
    if _is_duplicate_delivery(x_github_delivery):
        return {"ignored": "duplicate delivery"}

    background.add_task(run_agent, job)
    return {"accepted": True, "repo": job["repo"], "pr_number": job["pr_number"]}
