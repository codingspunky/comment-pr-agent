"""End-to-end orchestration tests with GitHub / git / LLM / validation faked.

The previous release claimed in its README that "the full LangGraph
orchestration (both routing modes, the retry loop, and retry-exhaustion) was
run with GitHub/Git/LLM/validation mocked and behaves correctly" -- but no such
harness existed in the tree, so the code with the worst bugs had no regression
protection at all. This is that harness.
"""
from __future__ import annotations

import pytest

from agent import graph as G
from agent.github_api import PullRequest
from agent.llm import TruncatedCompletion

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeAPI:
    """Records every call so tests can assert on GitHub side effects."""
    instances: list["FakeAPI"] = []

    def __init__(self, token, api_base=None):
        self.token = token
        FakeAPI.instances.append(self)

    pr_state = "open"
    head_repo = "octo/repo"
    comments: list[str] = []
    created_prs: list[dict] = []

    def get_default_branch(self, repo):
        return "main"

    def get_pull_request(self, repo, number):
        return PullRequest(number=number, head_ref="feature/x", base_ref="dev",
                           head_repo_full_name=FakeAPI.head_repo,
                           state=FakeAPI.pr_state,
                           html_url=f"https://github.com/{repo}/pull/{number}")

    def get_pull_request_diff(self, repo, number):
        return "diff --git a/x b/x"

    def create_pull_request(self, repo, title, body, head, base):
        FakeAPI.created_prs.append({"title": title, "head": head, "base": base, "body": body})
        return PullRequest(number=99, head_ref=head, base_ref=base,
                           head_repo_full_name=repo, state="open",
                           html_url=f"https://github.com/{repo}/pull/99")

    def request_reviewers(self, repo, number, reviewers):
        pass

    def create_comment(self, repo, issue_number, body):
        FakeAPI.comments.append(body)


class FakeGit:
    """Records git operations; maintains a tiny in-memory 'working tree'."""
    def __init__(self):
        self.reset_calls = 0
        self.committed_paths: list[list[str]] = []
        self.pushed: list[str] = []
        self.cloned = False

    def clone(self, repo, token, dest, api_host="github.com"):
        self.cloned = True
        return dest

    def configure_identity(self, path, name, email):
        pass

    def checkout_existing(self, path, branch, token):
        pass

    def checkout_new(self, path, base, new_branch, token):
        pass

    def reset_worktree(self, path):
        self.reset_calls += 1

    def diff_paths(self, path, paths, max_chars=6000):
        return "--- a/app/main.py\n+++ b/app/main.py\n+broken\n" if paths else ""

    def commit_paths(self, path, message, paths):
        self.committed_paths.append(list(paths))
        return "abc1234def" if paths else None

    def push(self, path, branch, token):
        self.pushed.append(branch)


class FakeValidation:
    def __init__(self, ok, output="ok"):
        self.ran = True
        self.ok = ok
        self.command = "pytest -q"
        self.output = output


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Wire the graph to fakes and return the handles tests assert on."""
    FakeAPI.instances = []
    FakeAPI.comments = []
    FakeAPI.created_prs = []
    FakeAPI.pr_state = "open"
    FakeAPI.head_repo = "octo/repo"

    fake_git = FakeGit()
    state = {
        "interpret": {"is_request": True, "wants_new_pr": False,
                      "instruction": "make the retry backoff exponential"},
        "plan": ({"target_files": ["app/main.py"], "approach": "edit the loop"}, [], ""),
        "edit_calls": [],
        "validations": [FakeValidation(True)],
    }

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("original\n", encoding="utf-8")

    monkeypatch.setattr(G, "GitHubAPI", FakeAPI)
    monkeypatch.setattr(G, "git_ops", fake_git)
    monkeypatch.setattr(G, "resolve_token", lambda iid, s=None: "ghs_faketoken")
    monkeypatch.setattr(G.llm, "interpret_comment",
                        lambda comment, has_open_pr, settings=None: state["interpret"])
    monkeypatch.setattr(G.llm, "plan_change",
                        lambda *a, **k: state["plan"])

    def fake_propose(instruction, approach, file_contents, **kw):
        state["edit_calls"].append({
            "file_contents": dict(file_contents),
            "feedback": kw.get("feedback"),
            "previous_attempt_diff": kw.get("previous_attempt_diff"),
        })
        return {"edits": [{"path": "app/main.py", "action": "write", "content": "patched\n"}],
                "summary": "make backoff exponential",
                "pr_title": "Make retry backoff exponential",
                "pr_body": "body"}

    monkeypatch.setattr(G.llm, "propose_changes", fake_propose)

    def fake_validate(repo_path, settings=None, command_override=None):
        # Read through `state` on every call: a test that reassigns
        # state["validations"] must be honoured, not shadowed by a list
        # captured when the fixture ran.
        validations = state["validations"]
        return validations[min(len(state["edit_calls"]) - 1, len(validations) - 1)]

    monkeypatch.setattr(G, "run_validation", fake_validate)
    return {"git": fake_git, "state": state, "path": tmp_path}


def _base_state(tmp_path, pr_number=None):
    return {
        "repo": "octo/repo",
        "installation_id": 1,
        "comment_body": "/agent make the retry backoff exponential",
        "comment_author": "alice",
        "issue_number": 7,
        "pr_number": pr_number,
        "repo_path": str(tmp_path),
        "workspace_preexisting": False,
    }


def _run(tmp_path, pr_number=None):
    g = G.build_graph()
    return g.invoke(_base_state(tmp_path, pr_number),
                    config={"configurable": {"thread_id": "t"}, "recursion_limit": 50})


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def test_existing_pr_mode_commits_without_opening_a_pr(wired):
    final = _run(wired["path"], pr_number=7)
    assert final["mode"] == "existing"
    assert final["work_branch"] == "feature/x"
    assert final["commit_sha"] == "abc1234def"
    assert wired["git"].pushed == ["feature/x"]
    assert FakeAPI.created_prs == []
    assert any("updates this PR" in c for c in FakeAPI.comments)


def test_no_pr_opens_a_new_pr_off_the_default_branch(wired):
    final = _run(wired["path"], pr_number=None)
    assert final["mode"] == "new"
    assert final["base_branch"] == "main"
    assert final["work_branch"].startswith("agent/make-the-retry-backoff-exp")
    assert len(FakeAPI.created_prs) == 1
    assert FakeAPI.created_prs[0]["base"] == "main"


def test_new_pr_from_inside_a_pr_targets_that_prs_base(wired):
    """Parallel to the PR under review, not stacked on it."""
    wired["state"]["interpret"]["wants_new_pr"] = True
    final = _run(wired["path"], pr_number=7)
    assert final["mode"] == "new"
    assert final["base_branch"] == "dev"
    assert FakeAPI.created_prs[0]["base"] == "dev"


def test_non_request_comment_replies_and_stops(wired):
    wired["state"]["interpret"] = {"is_request": False, "wants_new_pr": False, "instruction": ""}
    final = _run(wired["path"], pr_number=7)
    assert final["error"]
    assert wired["git"].pushed == []


def test_closed_pr_is_refused(wired):
    """The old code never checked pr.state and would push to a merged PR's
    branch, where nobody would see the change."""
    FakeAPI.pr_state = "closed"
    final = _run(wired["path"], pr_number=7)
    assert "closed" in final["error"]
    assert wired["git"].pushed == []


def test_fork_pr_is_refused_early(wired):
    FakeAPI.head_repo = "someone-else/repo"
    final = _run(wired["path"], pr_number=7)
    assert "fork" in final["error"]
    assert wired["git"].pushed == []


# --------------------------------------------------------------------------- #
# The retry loop
# --------------------------------------------------------------------------- #

def test_retry_resets_the_tree_and_shows_the_previous_attempt(wired):
    """The core correctness fix.

    Attempt 2 must (a) run against a reverted tree, and (b) be shown what
    attempt 1 actually did. The old loop did neither: it re-showed the pristine
    file with no diff while attempt 1's edits were still on disk.
    """
    wired["state"]["validations"] = [FakeValidation(False, "E   assert 1 == 2"),
                                     FakeValidation(True)]
    final = _run(wired["path"], pr_number=7)

    calls = wired["state"]["edit_calls"]
    assert len(calls) == 2, "expected exactly one retry"
    assert wired["git"].reset_calls == 1, "the working tree was not reverted before the retry"

    first, second = calls
    assert first["feedback"] is None and first["previous_attempt_diff"] is None
    assert "assert 1 == 2" in second["feedback"], "validation output was not fed back"
    assert "+broken" in second["previous_attempt_diff"], "the model was not shown its own attempt"
    # The contents shown are always the pristine originals -- the tree was reset.
    assert first["file_contents"] == second["file_contents"]
    assert final["commit_sha"] == "abc1234def"


def test_retry_exhaustion_reports_failure_with_output(wired):
    wired["state"]["validations"] = [FakeValidation(False, "boom-output")]
    final = _run(wired["path"], pr_number=7)
    assert final["error"]
    assert wired["git"].pushed == []
    assert any("boom-output" in c for c in FakeAPI.comments)
    assert wired["state"]["edit_calls"], "should have attempted at least one edit"
    assert wired["git"].reset_calls == G.SETTINGS.max_retries


# --------------------------------------------------------------------------- #
# Commit hygiene
# --------------------------------------------------------------------------- #

def test_commit_stages_only_the_touched_paths(wired):
    """Not `git add -A`: validation runs first and installs dependencies, so
    staging the whole tree commits build output."""
    _run(wired["path"], pr_number=7)
    assert wired["git"].committed_paths == [["app/main.py"]]


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #

def test_truncated_edit_fails_without_committing(wired, monkeypatch):
    def boom(*a, **k):
        raise TruncatedCompletion("hit the 4000-token output ceiling")

    monkeypatch.setattr(G.llm, "propose_changes", boom)
    final = _run(wired["path"], pr_number=7)
    assert "too large" in final["error"]
    assert wired["git"].pushed == []
    assert wired["git"].committed_paths == []


# --------------------------------------------------------------------------- #
# Guard integration
# --------------------------------------------------------------------------- #

def test_protected_target_files_are_filtered_before_editing(wired):
    """A planner that names `.git/config` gets nothing -- and the run stops
    rather than editing on air."""
    wired["state"]["plan"] = ({"target_files": [".git/config"], "approach": "peek"}, [], "")
    final = _run(wired["path"], pr_number=7)
    assert "protected" in final["error"]
    assert wired["state"]["edit_calls"] == []


def test_no_changes_produced_is_reported(wired, monkeypatch):
    monkeypatch.setattr(wired["git"], "commit_paths", lambda p, m, paths: None)
    final = _run(wired["path"], pr_number=7)
    assert final["error"] == "no changes were produced"


# --------------------------------------------------------------------------- #
# Listing determinism
# --------------------------------------------------------------------------- #

def test_repo_listing_is_sorted_and_flags_truncation(tmp_path):
    for i in range(30):
        (tmp_path / f"f{i:03d}.py").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")

    files, truncated = G._list_repo_files(str(tmp_path), limit=10)
    assert files == sorted(
        files), "an unsorted listing gives the planner a different slice each run"
    assert len(files) == 10 and truncated is True
    assert not any(f.startswith(".git/") for f in files)

    files, truncated = G._list_repo_files(str(tmp_path), limit=1000)
    assert truncated is False


def test_workspace_paths_do_not_collide():
    a = G.new_workspace_path("octo/repo")
    b = G.new_workspace_path("octo/repo")
    assert a != b, "two runs in the same second used to share a directory"


# --------------------------------------------------------------------------- #
# Validation claims must be true
# --------------------------------------------------------------------------- #

def test_skipped_validation_is_declared_not_hidden(wired):
    """`pr_body` is written by the model in the `edit` node, which runs BEFORE
    validation -- so anything it says about testing is invented. A real run
    produced "Validation: Ran pytest; all tests passed." on a PR where nothing
    ran. The engine states the truth instead."""
    class Skipped:
        ran, ok, command, output = False, True, "", "no validation command detected; skipped"

    wired["state"]["validations"] = [Skipped()]
    _run(wired["path"], pr_number=None)

    body = FakeAPI.created_prs[0]["body"]
    assert "**Validation:** none" in body
    assert "UNVERIFIED" in body


def test_passing_validation_names_the_command(wired):
    class Passed:
        ran, ok, command, output = True, True, "pytest -q", "3 passed"

    wired["state"]["validations"] = [Passed()]
    _run(wired["path"], pr_number=None)
    assert "**Validation:** passed — `pytest -q`" in FakeAPI.created_prs[0]["body"]


def test_validation_footer_counts_retries(wired):
    class Failed:
        ran, ok, command, output = True, False, "pytest -q", "1 failed"

    class Passed:
        ran, ok, command, output = True, True, "pytest -q", "3 passed"

    wired["state"]["validations"] = [Failed(), Passed()]
    _run(wired["path"], pr_number=None)
    assert "passed after 2 attempts" in FakeAPI.created_prs[0]["body"]
