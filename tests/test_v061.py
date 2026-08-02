"""v0.6.1: a no-op is an outcome, not a crash; and the CLI reports failures.

Two defects found by running the agent against a real repository:

  1. Asked to add a docstring that was already there, the model correctly
     returned zero edits with an explanation. `propose_changes` raised a bare
     RuntimeError, nothing caught it, and the run died with a stack trace
     printed TWICE. A reviewer asking for something already done is ordinary,
     so "already done" has to be a first-class terminal state.

  2. `run.py` called `run_agent` with no exception handling at all, so any
     exception the graph did not convert into an in-state error escaped to the
     top level as a traceback instead of the documented `FAILED: <reason>`.

Plus the display-only shortening of absolute interpreter paths in the PR footer.
"""
from __future__ import annotations

import pytest

from agent import graph as G
from agent import llm
from agent.llm import NoChangesNeeded, no_change_reason
from agent.sandbox import short_command

# --------------------------------------------------------------------------- #
# no_change_reason -- the pure decision
# --------------------------------------------------------------------------- #


def test_explicit_flag_is_honoured():
    args = {"edits": [], "no_changes_needed": True,
            "summary": "The docstring is present.", "pr_title": "", "pr_body": ""}
    assert no_change_reason(args) == "The docstring is present."


def test_explicit_flag_without_prose_still_qualifies():
    """The field is the signal; the explanation is a courtesy."""
    args = {"edits": [], "no_changes_needed": True}
    reason = no_change_reason(args)
    assert reason and "already present" in reason


def test_explicit_flag_trusted_even_when_edits_key_absent():
    assert no_change_reason({"no_changes_needed": True, "summary": "nothing doing"}) is not None


def test_prose_fallback_for_a_model_that_ignores_the_field():
    """The behaviour actually observed in production: no field, clear prose."""
    args = {"edits": [],
            "summary": "No changes needed.",
            "pr_body": "**What**: No changes needed.\n\n**Why**: app/main.py already "
                       "contains a module-level docstring."}
    assert no_change_reason(args) is not None


@pytest.mark.parametrize("text", [
    "Nothing to do — the function is already exported.",
    "No modifications are required for this request.",
    "The module already contains the requested docstring.",
    "This is already implemented in the base class.",
])
def test_prose_variants_recognised(text):
    assert no_change_reason({"edits": [], "summary": text}) is not None


def test_non_empty_edits_is_never_a_no_op():
    """Even with the flag set: if there are edits, apply them."""
    args = {"edits": [{"path": "a.py", "action": "write", "content": "x"}],
            "no_changes_needed": True, "summary": "no changes needed"}
    assert no_change_reason(args) is None


@pytest.mark.parametrize("args", [
    {},                                                   # nothing at all
    {"edits": []},                                        # empty, no explanation
    {"edits": [], "summary": "Added the missing docstring to app/db.py."},
    {"edits": [], "summary": "I could not complete this."},
    {"edits": None, "summary": "no changes needed"},       # malformed + right words
    {"edits": "oops", "summary": "nothing to do"},         # malformed + right words
])
def test_failures_are_not_mistaken_for_decisions(args):
    """The dangerous direction: a broken response read as a considered no-op.

    The last two matter most -- a model that failed to produce edits will still
    write confident prose, and `edits` not being a list is the tell that the
    response is broken rather than deliberate.
    """
    assert no_change_reason(args) is None


def test_bare_already_is_not_enough():
    """`already` on its own is too weak: 'already failing' is not a no-op."""
    assert no_change_reason({"edits": [], "summary": "The tests are already failing."}) is None


# --------------------------------------------------------------------------- #
# propose_changes -- which exception comes out
# --------------------------------------------------------------------------- #


def _patch_tool_result(monkeypatch, args):
    monkeypatch.setattr(llm, "_call_tool", lambda *a, **k: args)


def test_propose_changes_raises_no_changes_needed(monkeypatch):
    _patch_tool_result(monkeypatch, {"edits": [], "no_changes_needed": True,
                                     "summary": "already documented"})
    with pytest.raises(NoChangesNeeded) as exc:
        llm.propose_changes("add a docstring", "", {"a.py": "x"})
    assert exc.value.reason == "already documented"


def test_propose_changes_still_raises_on_a_broken_response(monkeypatch):
    _patch_tool_result(monkeypatch, {"edits": [], "summary": "Added the docstring."})
    with pytest.raises(RuntimeError) as exc:
        llm.propose_changes("add a docstring", "", {"a.py": "x"})
    assert not isinstance(exc.value, NoChangesNeeded)


def test_no_changes_needed_is_catchable_as_runtime_error(monkeypatch):
    """Subclassing keeps any older caller that caught RuntimeError working."""
    _patch_tool_result(monkeypatch, {"edits": [], "no_changes_needed": True, "summary": "done"})
    with pytest.raises(RuntimeError):
        llm.propose_changes("x", "", {})


# --------------------------------------------------------------------------- #
# The graph: edit node, routing, terminal node
# --------------------------------------------------------------------------- #


def test_edit_node_converts_it_into_state(monkeypatch):
    def boom(*a, **k):
        raise NoChangesNeeded("app/main.py already has a module-level docstring")
    monkeypatch.setattr(G.llm, "propose_changes", boom)

    out = G.edit({"repo_path": "/tmp/x", "instruction": "add a docstring",
                  "file_contents": {"app/main.py": "..."}})
    assert out["no_changes"] is True
    assert "already has" in out["no_changes_reason"]
    assert out.get("error") is None, "a no-op must not be reported as an error"


def test_router_sends_a_no_op_to_its_own_terminal():
    assert G.after_edit({"no_changes": True}) == "respond_no_changes"
    assert G.after_edit({"error": "boom"}) == "respond_failure"
    assert G.after_edit({}) == "validate"


def test_no_changes_wins_over_a_stray_error_flag():
    """Defensive: never route a no-op through the failure path."""
    assert G.after_edit({"no_changes": True, "error": "x"}) == "respond_no_changes"


def test_respond_no_changes_comments_and_does_not_set_error(monkeypatch):
    posted = []

    class API:
        def __init__(self, token, api_base=None):
            pass

        def create_comment(self, repo, issue_number, body):
            posted.append(body)

    monkeypatch.setattr(G, "GitHubAPI", API)
    out = G.respond_no_changes({"token": "t", "repo": "o/r", "issue_number": 1,
                                "no_changes_reason": "the docstring is already there"})
    assert out["done"] is True
    assert out.get("error") is None
    assert len(posted) == 1
    assert "No changes needed" in posted[0]
    assert "the docstring is already there" in posted[0]
    # No PR was opened, so the footer must not claim a validation result.
    assert "Validation:" not in posted[0]


def test_respond_no_changes_survives_a_failed_comment(monkeypatch):
    class API:
        def __init__(self, token, api_base=None):
            pass

        def create_comment(self, repo, issue_number, body):
            raise OSError("github down")

    monkeypatch.setattr(G, "GitHubAPI", API)
    out = G.respond_no_changes({"token": "t", "repo": "o/r", "issue_number": 1,
                                "no_changes_reason": "already done"})
    assert out["done"] is True


def test_no_changes_node_is_wired_into_the_graph():
    """A node that exists but is unreachable is the bug this catches."""
    compiled = G.build_graph()
    assert "respond_no_changes" in compiled.get_graph().nodes


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


_ARGV = ["--repo", "o/r", "--issue", "1", "--comment", "/agent do a thing"]


def _run_cli(monkeypatch, result=None, exc=None):
    from agent import run as run_mod

    def fake(job):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(run_mod, "run_agent", fake)
    with pytest.raises(SystemExit) as se:
        run_mod.main(_ARGV)
    return se.value.code


def test_cli_exits_zero_on_a_no_op(monkeypatch, capsys):
    code = _run_cli(monkeypatch, result={"no_changes": True,
                                         "no_changes_reason": "already there"})
    assert code == 0
    out = capsys.readouterr().out
    assert "NO CHANGES: already there" in out


def test_cli_reports_an_exception_as_one_line(monkeypatch, capsys):
    code = _run_cli(monkeypatch, exc=RuntimeError("the model returned no usable file edits"))
    assert code == 1
    err = capsys.readouterr().err
    assert "FAILED: RuntimeError: the model returned no usable file edits" in err
    assert "Traceback" not in err
    assert "--debug" in err


def test_cli_still_reports_an_in_state_error(monkeypatch, capsys):
    code = _run_cli(monkeypatch, result={"error": "validation did not pass"})
    assert code == 1
    assert "FAILED: validation did not pass" in capsys.readouterr().err


def test_cli_success_path_unchanged(monkeypatch, capsys):
    from agent import run as run_mod
    monkeypatch.setattr(run_mod, "run_agent",
                        lambda job: {"pr_url": "https://x/pull/1", "commit_sha": "abc123"})
    run_mod.main(_ARGV)          # must NOT raise SystemExit
    assert "OK: pr=https://x/pull/1" in capsys.readouterr().out


def test_cli_interrupt_is_not_reported_as_a_crash(monkeypatch, capsys):
    code = _run_cli(monkeypatch, exc=KeyboardInterrupt())
    assert code == 130
    assert "interrupted" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Footer path shortening
# --------------------------------------------------------------------------- #


def test_windows_venv_paths_are_shortened():
    cmd = (r'"C:\Users\Bhavya\AppData\Local\Temp\agent-validate-pes8lrl3\Scripts\python.exe"'
           r' -m pip install -q -r requirements.txt && '
           r'"C:\Users\Bhavya\AppData\Local\Temp\agent-validate-pes8lrl3\Scripts\python.exe"'
           r' -m pytest -q')
    out = short_command(cmd)
    assert "C:\\Users" not in out
    assert "Bhavya" not in out
    assert out == "python -m pip install -q -r requirements.txt && python -m pytest -q"


def test_posix_venv_paths_are_shortened():
    cmd = "/tmp/agent-validate-abc123/bin/python -m pytest -q"
    assert short_command(cmd) == "python -m pytest -q"


def test_a_plain_command_is_left_alone():
    for cmd in ("npm ci --silent && npm test --silent",
                "go test ./...",
                "python -m pytest -q",
                "make test"):
        assert short_command(cmd) == cmd


def test_non_interpreter_paths_survive():
    """Only the interpreter is rewritten; arguments are the reviewer's business."""
    out = short_command("python -m pytest -q tests/test_python_version.py")
    assert out.endswith("tests/test_python_version.py")


def test_empty_command_is_safe():
    assert short_command("") == ""


@pytest.mark.parametrize("cmd", [
    # Found by probing: `s:/` in `https://` looked like a drive letter and the
    # whole URL collapsed to `httppython-tools.whl`.
    "pip install https://example.com/python-tools.whl",
    "curl -sSL https://astral.sh/python/install.sh | sh",
    "python -m pytest -q --ignore=vendor/python",
    "bash scripts/python/run.sh",
])
def test_urls_and_relative_paths_are_not_touched(cmd):
    assert short_command(cmd) == cmd


def test_footer_uses_the_short_form():
    footer = G.validation_footer({
        "validation_ran": True, "validation_ok": True, "retries": 0,
        "validation_cmd": r'"C:\Users\me\AppData\Local\Temp\agent-validate-x\Scripts\python.exe"'
                          r' -m pytest -q',
    })
    assert "passed" in footer
    assert "C:\\Users" not in footer
    assert "`python -m pytest -q`" in footer
