"""Shared graph state."""
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict


class AgentState(TypedDict, total=False):
    # --- inputs (set by the trigger) ---
    repo: str                       # "owner/name"
    installation_id: Optional[int]
    comment_body: str
    comment_author: str
    issue_number: int               # the issue/PR number the comment lives on
    pr_number: Optional[int]        # set iff the comment is on an open PR
    force_new_pr: bool

    # --- workspace (chosen by the runner so cleanup always knows the path) ---
    repo_path: str
    workspace_preexisting: bool     # true under GitHub Actions (checkout ran)

    # --- derived ---
    instruction: str
    mode: str                       # "existing" | "new"
    base_branch: str
    work_branch: str
    token: str

    # --- skills (progressive disclosure) ---
    skills_index: str               # name: description lines for loadable skills
    skill_context: str              # always-on bodies + bodies the planner loaded
    active_skills: List[str]        # names the planner loaded on demand
    resolved_validate_cmd: str      # from a repo skill's metadata, if allowed

    # --- planning / editing ---
    repo_files: List[str]
    listing_truncated: bool
    diff: Optional[str]
    approach: str
    target_files: List[str]
    file_contents: Dict[str, str]   # PRISTINE contents; tree is reset per retry
    skipped_files: List[str]        # planner targets excluded by safety/size guards
    touched_files: List[str]        # paths the current attempt wrote or deleted
    previous_attempt_diff: str      # what the last failed attempt changed
    summary: str
    pr_title: str
    pr_body: str

    # --- validation loop ---
    validation_ok: bool
    validation_ran: bool            # False when no command was found/configured
    validation_cmd: str
    validation_output: str
    retries: int

    # --- outputs ---
    commit_sha: Optional[str]
    pr_url: Optional[str]
    error: Optional[str]
    done: bool

    # A terminal outcome that is neither success nor failure: the model read the
    # code and found the request already satisfied. Kept separate from `error`
    # so the CLI can exit 0 and the reviewer gets an explanation rather than a
    # report of something going wrong.
    no_changes: bool
    no_changes_reason: str
