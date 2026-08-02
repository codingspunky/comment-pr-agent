"""The LangGraph orchestrator.

Comment-triggered. A human says what to fix in a comment; the LLM interprets it
(no regex / keyword / template matching) and the shared core makes the change:

  ingest -> interpret -> setup -> gather -> discover_skills
        -> plan -> retrieve -> edit -> validate -> commit + publish
                     ^                    |
                     +--- reset_retry <---+   (failed & retries left)

The retry edge goes through `reset_retry`, which reverts the working tree.
Without that, attempt N+1 is applied on top of attempt N's files while the
model is shown the pristine originals -- so it cannot see its own last attempt,
and any file it created but did not resubmit survives into the commit.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Literal

from langgraph.graph import END, StateGraph

from . import git_ops, llm
from .commands import (
    apply_edits,
    decide_mode,
    edits_from_args,
    read_targets,
    redact_secrets,
    slugify,
)
from .config import get_settings
from .github_api import GitHubAPI
from .github_app import resolve_token
from .llm import NoChangesNeeded, PromptTooLarge, TruncatedCompletion
from .sandbox import short_command
from .sandbox import validate as run_validation
from .skills import SkillRegistry
from .state import AgentState

SETTINGS = get_settings()

_DEFAULT_IGNORE = {".git", "venv", ".venv", "node_modules", "__pycache__",
                   ".pytest_cache", ".mypy_cache", "build", "dist", ".tox", ".idea",
                   ".ruff_cache", ".gradle", "target", "vendor"}

FILE_LISTING_LIMIT = 2000


def _ignore_dirs() -> set[str]:
    extra = {d.strip() for d in os.environ.get("AGENT_IGNORE_DIRS", "").split(",") if d.strip()}
    return _DEFAULT_IGNORE | extra


def _list_repo_files(repo_path: str, limit: int = FILE_LISTING_LIMIT) -> tuple[list[str], bool]:
    """Deterministic, sorted listing of the repo.

    Returns (files, truncated). `os.walk` order is arbitrary and varies between
    runs, so an unsorted listing meant the planner saw a different slice of a
    large repo each time with no indication anything was missing. Sorting makes
    the slice stable, and the caller tells the model when it was cut.
    """
    ignore = _ignore_dirs()
    files: list[str] = []
    for root, dirs, names in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in ignore)
        for n in sorted(names):
            files.append(os.path.relpath(os.path.join(root, n), repo_path).replace(os.sep, "/"))
    files.sort()
    if len(files) > limit:
        return files[:limit], True
    return files, False


def _scrub(text: str) -> str:
    """Remove our own secrets from anything headed back to GitHub."""
    return redact_secrets(text or "", SETTINGS.secret_values())


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #

def ingest(state: AgentState) -> AgentState:
    """Structural setup only: authenticate. No parsing of the comment."""
    token = resolve_token(state.get("installation_id"), SETTINGS)
    return {"token": token, "retries": 0}


def interpret(state: AgentState) -> AgentState:
    """LLM interprets the comment: is it a request, new PR or not, and the task."""
    has_pr = state.get("pr_number") is not None
    result = llm.interpret_comment(state["comment_body"], has_open_pr=has_pr, settings=SETTINGS)

    if not result.get("is_request"):
        return {"error": "the comment is not a change request for the agent", "done": True}

    wants_new = bool(result.get("wants_new_pr"))
    instruction = (result.get("instruction") or "").strip()
    if not instruction:
        # is_request without a usable task -- refuse rather than plan on air.
        return {"error": "I couldn't extract a concrete task from the comment; "
                         "please rephrase what you want changed", "done": True}
    mode = decide_mode(state.get("pr_number"), wants_new)

    api = GitHubAPI(state["token"])
    pr_number = state.get("pr_number")
    if mode == "existing":
        # decide_mode() only returns "existing" when pr_number is not None, but
        # that invariant lives in another module. Bind it locally so the type is
        # provable here rather than by cross-file reasoning.
        assert pr_number is not None, "decide_mode guarantees a PR number here"
        pr = api.get_pull_request(state["repo"], pr_number)
        if pr.state != "open":
            # Committing to a merged or closed PR's branch resurrects a branch
            # nobody is watching; the reviewer would never see the change.
            return {"error": f"this pull request is {pr.state}, so I can't commit to it — "
                             f"ask for the change in a new PR instead "
                             f"(e.g. \"in a separate PR\")", "done": True}
        if pr.head_repo_full_name != state["repo"]:
            # Fork PR: the head branch lives in a repo this installation token
            # cannot push to. Fail early with a clear message instead of a
            # cryptic git error mid-run.
            return {"error": f"this PR's branch lives in a fork "
                             f"(`{pr.head_repo_full_name}`), which I can't push to — "
                             f"ask for the change in a new PR instead "
                             f"(e.g. \"in a separate PR\")", "done": True}
        base_branch, work_branch = pr.base_ref, pr.head_ref
    else:
        if pr_number is not None:
            # New PR raised from inside an open PR -> target that PR's BASE
            # (e.g. dev), so the fix PR is parallel to the one under review,
            # not stacked on it.
            base_branch = api.get_pull_request(state["repo"], pr_number).base_ref
        else:
            base_branch = api.get_default_branch(state["repo"])
        work_branch = f"agent/{slugify(instruction)}-{int(time.time())}"

    return {"instruction": instruction, "force_new_pr": wants_new, "mode": mode,
            "base_branch": base_branch, "work_branch": work_branch}


def setup_workspace(state: AgentState) -> AgentState:
    """Clone (or adopt an existing checkout) and select the working branch.

    `repo_path` is chosen by the caller (runner) and already present in state,
    so cleanup can find the directory even when a later node raises.
    """
    dest = state["repo_path"]
    if state.get("workspace_preexisting"):
        # GitHub Actions: actions/checkout already put the tree on disk.
        git_ops.configure_identity(dest, SETTINGS.git_author_name, SETTINGS.git_author_email)
        if state["mode"] == "new":
            git_ops.checkout_new(dest, state["base_branch"], state["work_branch"], state["token"])
        return {}
    git_ops.clone(state["repo"], state["token"], dest)
    git_ops.configure_identity(dest, SETTINGS.git_author_name, SETTINGS.git_author_email)
    if state["mode"] == "existing":
        git_ops.checkout_existing(dest, state["work_branch"], state["token"])
    else:
        git_ops.checkout_new(dest, state["base_branch"], state["work_branch"], state["token"])
    return {}


def gather_context(state: AgentState) -> AgentState:
    repo_files, truncated = _list_repo_files(state["repo_path"])
    diff = None
    pr_number = state.get("pr_number")
    if pr_number is not None:
        try:
            diff = GitHubAPI(state["token"]).get_pull_request_diff(state["repo"], pr_number)
        except Exception:
            diff = None
    return {"repo_files": repo_files, "listing_truncated": truncated, "diff": diff}


def discover_skills(state: AgentState) -> AgentState:
    """Discover builtin + repo skills; store only rendered strings (state must
    stay JSON-serialisable for the checkpointer -- no registry objects)."""
    reg = SkillRegistry.discover(state["repo_path"])
    repo_cmd = reg.validate_cmd() if SETTINGS.allow_repo_validate_cmd else None
    return {
        "skills_index": reg.render_index(),
        "skill_context": reg.render_always(),
        "active_skills": [],
        "resolved_validate_cmd": repo_cmd or "",
    }


def plan(state: AgentState) -> AgentState:
    """Plan under the skill index; the model may load skills (one bounded
    round). Loaded bodies join skill_context so the edit node and the retry
    loop work under the same procedure."""
    reg = SkillRegistry.discover(state["repo_path"])   # cheap fs walk; not in state
    try:
        result, loaded_names, loaded_context = llm.plan_change(
            state["instruction"], state["repo_files"], state.get("diff"),
            skills_index=state.get("skills_index", ""),
            always_skill_context=state.get("skill_context", ""),
            load_skill_bodies=reg.load,
            settings=SETTINGS,
            listing_truncated=bool(state.get("listing_truncated")),
        )
    except PromptTooLarge as exc:
        return {"error": f"I couldn't plan this change — {exc}", "done": True}
    context = state.get("skill_context", "")
    if loaded_context:
        context = (context + "\n\n" + loaded_context).strip()
    return {"approach": result.get("approach", ""),
            "target_files": result.get("target_files", []),
            "active_skills": loaded_names,
            "skill_context": context}


def retrieve(state: AgentState) -> AgentState:
    """Read the planner's target files with containment, protection and size
    guards. target_files is model output: never trust it with the filesystem."""
    contents, skipped = read_targets(
        state["repo_path"], state.get("target_files", []),
        max_files=SETTINGS.max_target_files, max_chars=SETTINGS.max_file_chars,
    )
    if not contents and skipped:
        return {"error": "none of the planned files could be safely edited: "
                         + "; ".join(skipped[:5]), "done": True,
                "file_contents": {}, "skipped_files": skipped}
    return {"file_contents": contents, "skipped_files": skipped}


def edit(state: AgentState) -> AgentState:
    feedback = state.get("validation_output") if state.get("validation_ok") is False else None
    constraints = ""
    if state.get("skipped_files"):
        constraints = ("These planned files were OMITTED and must NOT be modified "
                       "or recreated: " + "; ".join(state["skipped_files"]))
    try:
        result = llm.propose_changes(
            state["instruction"], state.get("approach", ""), state.get("file_contents", {}),
            skill_context=state.get("skill_context", ""),
            constraints=constraints,
            feedback=feedback,
            previous_attempt_diff=state.get("previous_attempt_diff") or None,
            settings=SETTINGS,
        )
    except TruncatedCompletion as exc:
        # Never write a partial file. Surface it as a normal failure reply.
        return {"error": f"the change was too large to produce safely — {exc}",
                "done": True}
    except PromptTooLarge as exc:
        return {"error": f"the change was too large to attempt safely — {exc}",
                "done": True}
    except NoChangesNeeded as exc:
        # Not an error. The model looked and found the work already done, which
        # is a legitimate answer to a reviewer's request. Terminating here skips
        # validation, the commit and the PR -- there is nothing to validate or
        # push -- and `respond_no_changes` tells the reviewer why.
        return {"no_changes": True, "no_changes_reason": exc.reason, "done": True}

    # A rejected path (protected or escaping) or a malformed edit entry is the
    # model getting it wrong, not the service breaking. Reporting the actual
    # reason beats the generic "internal error" the runner would otherwise
    # post, and it tells the reviewer what to rephrase.
    try:
        touched = apply_edits(state["repo_path"], edits_from_args(result.get("edits", [])))
    except ValueError as exc:
        return {"error": f"I produced an edit I'm not allowed to apply: {exc}",
                "done": True}
    return {
        "touched_files": touched,
        "summary": result.get("summary", "Applied requested change."),
        "pr_title": result.get("pr_title", state["instruction"][:72]),
        "pr_body": result.get("pr_body", state.get("approach", "")),
    }


def validate(state: AgentState) -> AgentState:
    result = run_validation(state["repo_path"], SETTINGS,
                            command_override=state.get("resolved_validate_cmd") or None)
    return {"validation_ok": result.ok,
            "validation_ran": result.ran,
            "validation_cmd": result.command,
            "validation_output": _scrub(result.output)}


def validation_footer(state: AgentState) -> str:
    """The truthful validation line, written by the engine.

    The model writes `pr_body` in the `edit` node, which runs BEFORE this ever
    executes -- so anything it says about testing is invented. A real run put
    "Validation: Ran pytest; all tests passed." on a PR where no validation
    command existed. The builtin pr-conventions skill now forbids the model
    from writing that section; this is the replacement.
    """
    if not state.get("validation_ran"):
        return ("\n\n**Validation:** none — no validation command was configured or "
                "detected for this repository, so these changes are UNVERIFIED. "
                "Set `AGENT_VALIDATE_CMD` (or enable a repo skill's `validate_cmd` "
                "with `AGENT_ALLOW_REPO_VALIDATE_CMD=true`) to gate the agent on "
                "your test suite.")
    # short_command is display-only: absolute temp-venv interpreter paths are
    # accurate but leak an operator's local filesystem into a public PR.
    cmd = short_command(state.get("validation_cmd") or "") or "the configured command"
    if state.get("validation_ok"):
        attempts = int(state.get("retries", 0)) + 1
        suffix = f" after {attempts} attempts" if attempts > 1 else ""
        return f"\n\n**Validation:** passed{suffix} — `{cmd}`"
    return f"\n\n**Validation:** FAILED — `{cmd}`"


def reset_retry(state: AgentState) -> AgentState:
    """Capture what the failed attempt did, then revert the tree.

    Two jobs in one node:
      1. record the diff so the next edit prompt can show the model its own
         previous attempt instead of making it re-derive from the original;
      2. `git reset --hard && git clean -fdx`, so the next attempt starts from
         exactly the contents it is shown, and nothing a discarded attempt
         created (or any dependency the validation step installed) can survive
         into the commit.
    """
    diff = git_ops.diff_paths(state["repo_path"], state.get("touched_files", []))
    git_ops.reset_worktree(state["repo_path"])
    return {"retries": state.get("retries", 0) + 1,
            "previous_attempt_diff": diff,
            "touched_files": []}


def commit_push(state: AgentState) -> AgentState:
    msg = (f"{state.get('summary', 'agent change')}\n\n"
           f"Requested via comment by @{state.get('comment_author', 'unknown')}")
    sha = git_ops.commit_paths(state["repo_path"], msg, state.get("touched_files", []))
    if sha is None:
        return {"commit_sha": None, "error": "no changes were produced", "done": True}
    git_ops.push(state["repo_path"], state["work_branch"], state["token"])
    return {"commit_sha": sha}


def open_pr(state: AgentState) -> AgentState:
    api = GitHubAPI(state["token"])
    try:
        pr = api.create_pull_request(
            state["repo"], state.get("pr_title") or state["instruction"][:72],
            (state.get("pr_body") or "")
            + validation_footer(state)
            + f"\n\n---\nRequested via comment by @{state.get('comment_author','')}.",
            head=state["work_branch"], base=state["base_branch"],
        )
    except Exception as exc:
        # The branch is already pushed; say so rather than losing the work.
        return {"error": f"the commit was pushed to `{state['work_branch']}` but opening "
                         f"the pull request failed ({type(exc).__name__}). "
                         f"You can open it manually from that branch.",
                "done": True}
    return {"pr_url": pr.html_url, "pr_number": pr.number}


def request_review(state: AgentState) -> AgentState:
    pr_number = state.get("pr_number")
    if SETTINGS.default_reviewers and pr_number is not None:
        try:
            GitHubAPI(state["token"]).request_reviewers(
                state["repo"], pr_number, SETTINGS.default_reviewers)
        except Exception:
            pass
    return {}


def respond_success(state: AgentState) -> AgentState:
    api = GitHubAPI(state["token"])
    if state["mode"] == "existing":
        body = (f"Done — committed `{(state.get('commit_sha') or '')[:7]}` to "
                f"`{state['work_branch']}`, which updates this PR.\n\n"
                f"**Change:** {state.get('summary','')}")
    else:
        body = f"Done — opened {state.get('pr_url')}\n\n**Change:** {state.get('summary','')}"
    body += validation_footer(state)
    try:
        api.create_comment(state["repo"], state["issue_number"], _scrub(body))
    except Exception:
        # The push/PR already succeeded -- a failed reply is log-worthy, not fatal.
        pass
    return {"done": True}


def respond_no_changes(state: AgentState) -> AgentState:
    """Terminal, and deliberately NOT a failure.

    Nothing was committed and no PR was opened, so there is no validation
    footer to write -- claiming a validation result here would be exactly the
    invented-verification problem `validation_footer` exists to prevent.
    """
    api = GitHubAPI(state["token"])
    reason = state.get("no_changes_reason") or "the requested change is already present"
    body = (f"No changes needed — {reason}\n\n"
            f"I read the file(s) and did not open a pull request, because there was "
            f"nothing to change. If you expected an edit here, rephrase the request "
            f"or point me at a different file.")
    try:
        api.create_comment(state["repo"], state["issue_number"], _scrub(body))
    except Exception:
        pass
    return {"done": True, "no_changes": True, "no_changes_reason": reason}


def respond_failure(state: AgentState) -> AgentState:
    api = GitHubAPI(state["token"])
    reason = state.get("error") or "validation did not pass after the configured retries"
    tail = (state.get("validation_output") or "")[-1500:]
    body = f"I couldn't complete this safely: {reason}."
    if tail:
        body += (f"\n\n<details><summary>validation output</summary>\n\n"
                 f"```\n{tail}\n```\n</details>")
    try:
        api.create_comment(state["repo"], state["issue_number"], _scrub(body))
    except Exception:
        pass
    return {"done": True, "error": reason}


# --------------------------------------------------------------------------- #
# Routers
# --------------------------------------------------------------------------- #

def after_interpret(state: AgentState) -> Literal["setup_workspace", "respond_failure"]:
    return "respond_failure" if state.get("error") else "setup_workspace"


def after_plan(state: AgentState) -> Literal["retrieve", "respond_failure"]:
    """The plan node can now fail cleanly (PromptTooLarge). Without this edge
    an errored plan fell through to `retrieve` with no target_files, and the
    edit call ran against an empty file set."""
    return "respond_failure" if state.get("error") else "retrieve"


def after_retrieve(state: AgentState) -> Literal["edit", "respond_failure"]:
    return "respond_failure" if state.get("error") else "edit"


def after_edit(state: AgentState) -> Literal["validate", "respond_no_changes", "respond_failure"]:
    if state.get("no_changes"):
        return "respond_no_changes"
    return "respond_failure" if state.get("error") else "validate"


def after_validate(state: AgentState) -> Literal["commit_push", "reset_retry", "respond_failure"]:
    if state.get("validation_ok"):
        return "commit_push"
    if state.get("retries", 0) < SETTINGS.max_retries:
        return "reset_retry"
    return "respond_failure"


def after_commit(state: AgentState) -> Literal["open_pr", "request_review", "respond_failure"]:
    if state.get("error"):
        return "respond_failure"
    return "open_pr" if state["mode"] == "new" else "request_review"


def after_open_pr(state: AgentState) -> Literal["request_review", "respond_failure"]:
    return "respond_failure" if state.get("error") else "request_review"


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    for name, fn in [
        ("ingest", ingest), ("interpret", interpret), ("setup_workspace", setup_workspace),
        ("gather_context", gather_context), ("discover_skills", discover_skills),
        ("plan", plan), ("retrieve", retrieve),
        ("reset_retry", reset_retry), ("edit", edit), ("validate", validate),
        ("commit_push", commit_push), ("open_pr", open_pr), ("request_review", request_review),
        ("respond_success", respond_success), ("respond_failure", respond_failure),
        ("respond_no_changes", respond_no_changes),
    ]:
        g.add_node(name, fn)

    g.set_entry_point("ingest")
    g.add_edge("ingest", "interpret")
    g.add_conditional_edges("interpret", after_interpret,
                            {"setup_workspace": "setup_workspace",
                             "respond_failure": "respond_failure"})
    g.add_edge("setup_workspace", "gather_context")
    g.add_edge("gather_context", "discover_skills")
    g.add_edge("discover_skills", "plan")
    g.add_conditional_edges("plan", after_plan,
                            {"retrieve": "retrieve", "respond_failure": "respond_failure"})
    g.add_conditional_edges("retrieve", after_retrieve,
                            {"edit": "edit", "respond_failure": "respond_failure"})
    g.add_conditional_edges("edit", after_edit,
                            {"validate": "validate",
                             "respond_no_changes": "respond_no_changes",
                             "respond_failure": "respond_failure"})
    g.add_conditional_edges("validate", after_validate,
                            {"commit_push": "commit_push", "reset_retry": "reset_retry",
                             "respond_failure": "respond_failure"})
    g.add_edge("reset_retry", "edit")
    g.add_conditional_edges("commit_push", after_commit,
                            {"open_pr": "open_pr", "request_review": "request_review",
                             "respond_failure": "respond_failure"})
    g.add_conditional_edges("open_pr", after_open_pr,
                            {"request_review": "request_review",
                             "respond_failure": "respond_failure"})
    g.add_edge("request_review", "respond_success")
    g.add_edge("respond_success", END)
    g.add_edge("respond_failure", END)
    g.add_edge("respond_no_changes", END)
    return g.compile(checkpointer=checkpointer)


def new_workspace_path(repo: str) -> str:
    """Collision-free workspace directory.

    The old scheme used a whole-second timestamp, so two runs on the same repo
    in the same second collided -- and `clone` rmtree's the destination first,
    so one run would delete the other's tree mid-flight.
    """
    return os.path.join(SETTINGS.workdir,
                        f"{repo.replace('/', '_')}-{uuid.uuid4().hex[:12]}")
