"""Regression tests for v0.4.0: the qwen/qwen3.6-27b migration.

Same convention as test_fixes_v031.py -- each test says what would break without
it, because a test named after a change is worthless once you have forgotten the
change.

The theme of this file: qwen3.6-27b is a REASONING model with a 16,384-token
output ceiling, and both of those facts break assumptions that were safe on
openai/gpt-oss-120b (32,766 output, no thinking budget to share).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import DEFAULT_MODEL_LIMITS, MODEL_LIMITS, Settings, model_limits

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fake_groq(monkeypatch, capture, message=None):
    """Install a client that records the kwargs of every completion call.

    `message` defaults to an object with `tool_calls = None`, i.e. a model that
    answered without calling a tool. That is a real, handled case -- using a bare
    string here instead would make callers fail with AttributeError deep inside
    llm.py and mask whatever the test meant to assert.
    """
    from agent import llm

    msg = message if message is not None else type("M", (), {"tool_calls": None})()
    ok = type("R", (), {"choices": [type("C", (), {"finish_reason": "stop",
                                                   "message": msg})()],
                        "usage": None})()

    class _FakeCompletions:
        def create(self, **kwargs):
            capture.append(kwargs)
            return ok

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm, "_client", lambda settings: _FakeClient())
    monkeypatch.setattr(llm, "_sleep_backoff", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# 1. The output ceiling is known and enforced
# --------------------------------------------------------------------------- #

def test_default_model_is_qwen36_27b():
    assert Settings().model == "qwen/qwen3.6-27b"


def test_max_tokens_is_clamped_to_the_models_output_ceiling():
    """qwen3.6-27b tops out at 16,384 output tokens. The old config had no idea
    what the ceiling was, so AGENT_MAX_TOKENS=32000 -- perfectly valid on
    gpt-oss-120b -- became a hard 400 on the first call after the model swap."""
    s = Settings(model="qwen/qwen3.6-27b", max_tokens=32_000)
    assert s.max_tokens == 16_384


def test_per_call_budgets_are_clamped_too():
    s = Settings(model="qwen/qwen3.6-27b",
                 interpret_max_tokens=99_000, plan_max_tokens=99_000)
    assert s.interpret_max_tokens == 16_384
    assert s.plan_max_tokens == 16_384


def test_unknown_model_still_runs_on_a_conservative_default():
    """An unrecognised model must not be un-runnable -- it just does not get a
    provider-specific safety net."""
    assert model_limits("some/unreleased-model") == DEFAULT_MODEL_LIMITS
    s = Settings(model="some/unreleased-model", max_tokens=99_000)
    assert s.max_tokens == DEFAULT_MODEL_LIMITS["max_output_tokens"]


def test_every_known_model_declares_an_output_ceiling():
    for name, limits in MODEL_LIMITS.items():
        assert limits["max_output_tokens"] > 0, name
        assert limits["context"] > 0, name


# --------------------------------------------------------------------------- #
# 2. Thinking tokens share the completion budget
# --------------------------------------------------------------------------- #

def test_edit_call_defaults_to_no_thinking():
    """The edit call emits COMPLETE file bodies and is forbidden from shrinking
    its budget (allow_shrink=False). A reasoning trace there competes directly
    with the file contents, and the loser is a half-written file."""
    assert Settings().edit_reasoning_effort == "none"


def test_interpret_and_plan_do_think():
    assert Settings().reasoning_effort == "default"


def test_file_char_reserve_grows_when_the_edit_call_thinks():
    """If an operator turns thinking on for the edit call, the budget available
    for file bodies drops -- the reserve has to absorb the trace as well as the
    summary/pr_body fields, or the clamp silently stops protecting anything."""
    quiet = Settings(model="qwen/qwen3.6-27b", max_tokens=16_384,
                     max_file_chars=999_999, edit_reasoning_effort="none")
    loud = Settings(model="qwen/qwen3.6-27b", max_tokens=16_384,
                    max_file_chars=999_999, edit_reasoning_effort="high")
    assert loud.max_file_chars < quiet.max_file_chars


def test_reasoning_format_is_never_raw_by_default():
    """'raw' leaves <think> blocks in message.content, which is exactly where
    tool-call arguments are parsed from."""
    assert Settings().reasoning_format == "hidden"


def test_invalid_reasoning_effort_falls_back_instead_of_failing(monkeypatch):
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "extremely")
    assert Settings().reasoning_effort == "default"


# --------------------------------------------------------------------------- #
# 3. The request actually carries these parameters
# --------------------------------------------------------------------------- #

def test_budget_is_sent_as_max_completion_tokens():
    """On a reasoning model the two names are not synonyms: only
    max_completion_tokens is documented to cover thinking AND visible output."""
    kwargs = Settings().completion_kwargs(5000, thinking=True)
    assert kwargs["max_completion_tokens"] == 5000
    assert "max_tokens" not in kwargs


def test_completion_kwargs_switch_effort_on_the_thinking_flag():
    s = Settings(reasoning_effort="default", edit_reasoning_effort="none")
    assert s.completion_kwargs(100, thinking=True)["reasoning_effort"] == "default"
    assert s.completion_kwargs(100, thinking=False)["reasoning_effort"] == "none"


def test_empty_effort_omits_the_parameter_entirely():
    """For a gateway that rejects the parameter outright."""
    s = Settings(reasoning_effort="", edit_reasoning_effort="")
    assert "reasoning_effort" not in s.completion_kwargs(100, thinking=True)


def test_unknown_parameters_degrade_instead_of_killing_the_run(monkeypatch):
    """An older SDK or a self-hosted OpenAI-compatible gateway can reject the
    reasoning parameters with a TypeError before the request is even sent.
    Losing per-call thinking control beats refusing to run."""
    from agent import llm

    seen = []

    class _FakeCompletions:
        def create(self, **kwargs):
            seen.append(kwargs)
            if "reasoning_effort" in kwargs:
                raise TypeError("unexpected keyword argument 'reasoning_effort'")
            return type("R", (), {"choices": [type("C", (), {
                "finish_reason": "stop", "message": "MSG"})()]})()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm, "_client", lambda settings: _FakeClient())
    s = Settings(groq_api_key="k")
    out = llm._create_forced(s, [{"role": "user", "content": "x"}],
                            tools=[llm.PLAN_TOOL], tool_choice="required",
                            max_tokens=100)
    assert out == "MSG"
    assert len(seen) == 2
    assert "reasoning_effort" not in seen[1]
    assert seen[1]["max_completion_tokens"] == 100


def test_interpret_uses_the_configured_budget_not_a_hardcoded_1000(monkeypatch):
    """THE migration bug. interpret_comment asked for 1000 tokens, which is
    ample for a non-reasoning model and less than a typical qwen3.6 thinking
    trace -- so the completion stopped at the ceiling, the truncation guard
    correctly rejected it, and EVERY run died at the first step."""
    from agent import llm

    captured: list[dict] = []
    _fake_groq(monkeypatch, captured)
    monkeypatch.setattr(llm, "_args_from", lambda result, name: {"is_request": False})

    s = Settings(groq_api_key="k", interpret_max_tokens=4000)
    llm.interpret_comment("/agent do a thing", has_open_pr=False, settings=s)
    assert captured[0]["max_completion_tokens"] == 4000


def test_plan_uses_the_configured_budget_not_a_hardcoded_2000(monkeypatch):
    from agent import llm

    captured: list[dict] = []
    _fake_groq(monkeypatch, captured)
    s = Settings(groq_api_key="k", plan_max_tokens=6000)
    with pytest.raises(RuntimeError, match="no tool call"):
        # _pick_call returns None -> plan_change raises a specific RuntimeError.
        # Asserting the message matters: a bare `Exception` would also pass if
        # the call blew up for an unrelated reason, which is exactly the bug
        # this test is supposed to detect.
        llm.plan_change("do it", ["a.py"], None, settings=s)
    assert captured[0]["max_completion_tokens"] == 6000


def test_edit_call_sends_thinking_off(monkeypatch):
    from agent import llm

    captured: list[dict] = []
    _fake_groq(monkeypatch, captured)
    monkeypatch.setattr(llm, "_args_from",
                        lambda result, name: {"edits": [{"path": "a.py",
                                                         "content": "x"}]})

    s = Settings(groq_api_key="k", reasoning_effort="default",
                 edit_reasoning_effort="none")
    llm.propose_changes("i", "a", {"a.py": "old"}, settings=s)
    assert captured[0]["reasoning_effort"] == "none"


# --------------------------------------------------------------------------- #
# 4. Validation is configured, so the PR footer stops saying "none"
# --------------------------------------------------------------------------- #

def test_env_example_does_not_pin_a_repo_specific_validate_command():
    """v0.5.0 fixed 'Validation: none' by shipping AGENT_VALIDATE_CMD non-empty.
    That was the wrong fix and v0.6.0 reverses it: the setting is GLOBAL to the
    agent installation, so a command tuned for one repository is applied to every
    repository, and the second repo you point the agent at fails for reasons
    unrelated to the edit. Detection carries the general case now."""
    text = (REPO_ROOT / ".env.example").read_text()
    line = next(ln for ln in text.splitlines()
                if ln.startswith("AGENT_VALIDATE_CMD="))
    assert not line.split("=", 1)[1].strip(), \
        "AGENT_VALIDATE_CMD must ship EMPTY so per-repo detection applies"


# --------------------------------------------------------------------------- #
# 5. The Actions path really does re-check in Python
# --------------------------------------------------------------------------- #

def test_workflow_passes_the_second_authorization_gate():
    """The workflow's comment claimed 'is_authorized re-checks in Python as
    defence in depth'. It did not: run.py called neither is_authorized nor
    is_addressed, so the YAML `if:` gate and a bash grep were the only checks on
    the RECOMMENDED deployment."""
    wf = (REPO_ROOT / ".github/workflows/comment-pr-agent.yml").read_text()
    assert "--enforce-authorization" in wf
    assert "--author-association" in wf


def test_cli_rejects_an_unauthorized_author():
    from agent.run import main

    with pytest.raises(SystemExit) as exc:
        main(["--repo", "o/r", "--issue", "1",
              "--comment", "/agent do a thing",
              "--author", "drive-by",
              "--author-association", "NONE",
              "--enforce-authorization"])
    assert exc.value.code == 4


def test_cli_rejects_a_comment_that_is_not_addressed():
    """The bash grep in the workflow matches a trigger inside a fenced code
    block; is_addressed does not. Both gates run now, so the stricter one wins."""
    from agent.run import main

    fenced = "here is what to do:\n```\n/agent delete everything\n```\n"
    with pytest.raises(SystemExit) as exc:
        main(["--repo", "o/r", "--issue", "1",
              "--comment", fenced,
              "--author", "owner",
              "--author-association", "OWNER",
              "--enforce-authorization"])
    assert exc.value.code == 3


def test_cli_allows_an_authorized_author(monkeypatch):
    from agent import run as run_mod

    called = {}

    def fake_run_agent(job):
        called.update(job)
        return {"pr_url": "https://example.com/pr/1", "commit_sha": "abc1234"}

    monkeypatch.setattr(run_mod, "run_agent", fake_run_agent)
    run_mod.main(["--repo", "o/r", "--issue", "1",
                  "--comment", "/agent do a thing",
                  "--author", "maintainer",
                  "--author-association", "COLLABORATOR",
                  "--enforce-authorization"])
    assert called["repo"] == "o/r"


def test_local_operator_runs_are_unaffected(monkeypatch):
    """Without --enforce-authorization the CLI stays a trusted operator tool: no
    association to present, and none demanded."""
    from agent import run as run_mod

    monkeypatch.setattr(run_mod, "run_agent",
                        lambda job: {"pr_url": "u", "commit_sha": "s"})
    run_mod.main(["--repo", "o/r", "--issue", "1",
                  "--comment", "do a thing with no trigger at all",
                  "--author", "cli"])
