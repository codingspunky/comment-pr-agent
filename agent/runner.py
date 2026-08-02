"""Invoke the compiled graph for a single job. Shared by the webhook and the CLI."""
from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache

from . import usage as usage_mod
from .config import get_settings
from .graph import build_graph, new_workspace_path
from .sandbox import cleanup_validation_env
from .state import AgentState

log = logging.getLogger("comment-pr-agent")
SETTINGS = get_settings()


@lru_cache(maxsize=1)
def get_checkpointer():
    """Built once per process.

    The old code constructed one per job and called `__enter__()` on the
    Postgres saver without a matching `__exit__`, leaking a connection on every
    webhook.
    """
    if SETTINGS.database_url:
        from langgraph.checkpoint.postgres import PostgresSaver
        saver = PostgresSaver.from_conn_string(SETTINGS.database_url).__enter__()
        saver.setup()
        return saver
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


@lru_cache(maxsize=1)
def get_graph():
    """Compiled once per process rather than per job."""
    return build_graph(checkpointer=get_checkpointer())


def _best_effort_failure_comment(job: dict) -> None:
    """An unhandled exception must not leave the reviewer staring at silence.
    Best-effort: resolve a token and reply; swallow everything if that fails."""
    try:
        from .github_api import GitHubAPI
        from .github_app import resolve_token
        token = resolve_token(job.get("installation_id"), SETTINGS)
        GitHubAPI(token).create_comment(
            job["repo"], int(job["issue_number"]),
            "I hit an internal error and couldn't complete this request. "
            "The service logs have details.")
    except Exception:
        log.warning("could not post failure comment for %s", job.get("repo"))


def run_agent(job: dict) -> AgentState:
    """Run the agent end to end. `job` comes from the webhook, the CLI, or a
    GitHub Actions step."""
    graph = get_graph()

    # Chosen HERE, not inside the graph, so the `finally` below can always find
    # the directory to clean up -- previously repo_path was read off the final
    # state, so any exception leaked the whole clone into /tmp.
    preexisting = bool(job.get("workspace"))
    repo_path = job.get("workspace") or new_workspace_path(job["repo"])

    initial: AgentState = {
        "repo": job["repo"],
        "installation_id": int(job["installation_id"]) if job.get("installation_id") else None,
        "comment_body": job["comment_body"],
        "comment_author": job.get("comment_author", ""),
        "issue_number": int(job["issue_number"]),
        "pr_number": int(job["pr_number"]) if job.get("pr_number") else None,
        "force_new_pr": False,
        "repo_path": repo_path,
        "workspace_preexisting": preexisting,
    }

    # Stable per (repo, issue, delivery) so a resume lands on the same thread.
    # The old scheme appended a random uuid, which made the "idempotent
    # resumes" the docstring promised impossible.
    thread_id = job.get("delivery_id") or f"{job['repo']}#{job['issue_number']}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}

    # Token accounting is scoped to this run. ContextVar rather than a module
    # global because the webhook runs jobs as concurrent background tasks in one
    # process -- a global accumulator would blend two reviewers' runs together.
    usage_token = usage_mod.start_run(thread_id)
    try:
        final = graph.invoke(initial, config=config)
        if final.get("error"):
            log.warning("agent finished with error: %s", final["error"])
        else:
            log.info("agent done: pr=%s commit=%s", final.get("pr_url"), final.get("commit_sha"))
        return final
    except Exception as exc:
        # The traceback moves to DEBUG. At INFO the operator gets one readable
        # line, and the CLI adds `FAILED: <reason>` on top of it -- previously
        # this printed a full traceback here AND a second, uncaught one from
        # `run.py`, which made an ordinary failure look like a crashed process.
        # `--debug` (or any DEBUG-level config) brings the traceback back.
        log.error("agent run failed for %s: %s: %s",
                  job.get("repo"), type(exc).__name__, exc)
        log.debug("traceback for the failure above", exc_info=True)
        _best_effort_failure_comment(job)
        raise
    finally:
        # Logged in `finally` on purpose: a run that DIED is exactly the one whose
        # token spend you want to see, and on a small TPM budget a failed run
        # costs the same as a successful one.
        usage_mod.end_run(usage_token, SETTINGS)
        # The validation virtualenv lives outside the worktree (git clean -fdx
        # would delete one inside it), so removing the workspace does not remove
        # it. Cleaned here, where the run definitely ends.
        if repo_path:
            cleanup_validation_env(repo_path)
        # Never delete a workspace we did not create (Actions' checkout).
        if not preexisting and repo_path and os.path.isdir(repo_path):
            shutil.rmtree(repo_path, ignore_errors=True)
