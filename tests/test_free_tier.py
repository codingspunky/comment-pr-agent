"""Regression tests for v0.5.0: surviving a small token-per-minute budget.

The premise: one run of this agent costs more tokens than a free-tier minute
allows, so a 429 mid-run is the NORMAL path. Everything here defends that.
"""
from __future__ import annotations

from pathlib import Path

from agent import usage as usage_mod
from agent.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Resp:
    def __init__(self, headers):
        self.headers = headers


class _Err(Exception):
    def __init__(self, headers):
        self.response = _Resp(headers)


# --------------------------------------------------------------------------- #
# 1. retry-after is read, not ignored
# --------------------------------------------------------------------------- #

def test_retry_after_seconds_is_used():
    """The fixed backoff tops out at 8s. A TPM window is 60s. Ignoring the
    header meant giving up ~45 seconds early on a request that would have
    succeeded."""
    from agent.llm import _retry_after_seconds
    assert _retry_after_seconds(_Err({"retry-after": "30"})) == 30.0


def test_retry_after_parses_duration_suffixes():
    from agent.llm import _retry_after_seconds
    assert _retry_after_seconds(_Err({"retry-after": "7.66s"})) == 7.66
    assert _retry_after_seconds(_Err({"retry-after": "500ms"})) == 0.5
    assert _retry_after_seconds(_Err({"retry-after": "1m2s"})) == 62.0


def test_rate_limit_reset_header_is_a_fallback():
    from agent.llm import _retry_after_seconds
    assert _retry_after_seconds(_Err({"x-ratelimit-reset-tokens": "12s"})) == 12.0


def test_unparseable_or_absent_header_falls_back_to_backoff():
    from agent.llm import _retry_after_seconds
    assert _retry_after_seconds(_Err({"retry-after": "soon"})) is None
    assert _retry_after_seconds(_Err({})) is None
    assert _retry_after_seconds(Exception("no response attr")) is None


def test_server_wait_is_capped():
    """A malformed or hostile header must not park a run for an hour."""
    from agent import llm
    slept = []
    original = llm.time.sleep
    try:
        llm.time.sleep = slept.append
        llm._sleep_backoff(1, _Err({"retry-after": "99999"}))
    finally:
        llm.time.sleep = original
    assert slept and slept[0] <= llm.MAX_SERVER_WAIT + 1


def test_transient_retry_budget_is_configurable():
    """Free tier needs more patience than the default."""
    assert Settings(max_transient_retries=6).max_transient_retries == 6


# --------------------------------------------------------------------------- #
# 2. Usage accounting
# --------------------------------------------------------------------------- #

class _Usage:
    def __init__(self, prompt, completion, reasoning=0, cached=0):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.completion_tokens_details = type("D", (), {"reasoning_tokens": reasoning})()
        self.prompt_tokens_details = type("D", (), {"cached_tokens": cached})()


def test_usage_records_reasoning_tokens():
    """The entire argument for disabling thinking on the edit call is that
    reasoning tokens share the answer's ceiling. This is the measurement that
    turns that argument into a number."""
    token = usage_mod.start_run("t1")
    try:
        call = usage_mod.record("plan", _Usage(500, 200, reasoning=150), 2000, True)
        assert call is not None
        assert call.reasoning_tokens == 150
        assert usage_mod.current().total("reasoning_tokens") == 150
    finally:
        usage_mod.end_run(token)


def test_rate_limit_cost_uses_requested_not_used():
    """The provider charges prompt + REQUESTED budget against TPM, so a call
    that returns 200 tokens while requesting 12,000 still spends 12,000."""
    token = usage_mod.start_run("t2")
    try:
        call = usage_mod.record("edit", _Usage(1000, 200), 12000, False)
        assert call.rate_limit_cost == 13000
    finally:
        usage_mod.end_run(token)


def test_cached_tokens_are_excluded_from_rate_limit_cost():
    """Cached prefixes don't count toward TPM -- which is why the number is
    worth logging: if it stays zero, prompt restructuring is free headroom."""
    token = usage_mod.start_run("t3")
    try:
        call = usage_mod.record("plan", _Usage(1000, 100, cached=800), 2000, True)
        assert call.rate_limit_cost == 2200
    finally:
        usage_mod.end_run(token)


def test_recording_outside_a_run_is_a_no_op():
    assert usage_mod.record("x", _Usage(1, 1), 10, True) is None


def test_malformed_usage_never_breaks_a_run():
    """Instrumentation must not be able to fail the thing it measures."""
    token = usage_mod.start_run("t4")
    try:
        assert usage_mod.record("x", object(), 10, True) is not None
        assert usage_mod.record("y", None, 10, True) is not None
    finally:
        usage_mod.end_run(token)


def test_summary_totals_across_calls():
    token = usage_mod.start_run("t5")
    try:
        usage_mod.record("interpret", _Usage(300, 100), 800, False)
        usage_mod.record("edit", _Usage(1700, 900), 3000, False)
        summary = usage_mod.current().summary()
        assert summary["llm_calls"] == 2
        assert summary["prompt_tokens"] == 2000
        assert summary["rate_limit_cost"] == 5800
    finally:
        usage_mod.end_run(token)


def test_cost_is_not_estimated_without_configured_prices():
    """No stale price table baked into source -- silence beats a confident
    wrong number."""
    token = usage_mod.start_run("t6")
    try:
        usage_mod.record("edit", _Usage(1000, 1000), 1000, False)
        run = usage_mod.current()
        assert usage_mod.estimate_cost(run, Settings()) is None
        priced = Settings(price_input_per_m=0.60, price_output_per_m=3.00)
        assert abs(usage_mod.estimate_cost(run, priced) - 0.0036) < 1e-9
    finally:
        usage_mod.end_run(token)


def test_runs_do_not_bleed_into_each_other():
    """The webhook runs jobs as concurrent background tasks in one process."""
    outer = usage_mod.start_run("outer")
    usage_mod.record("a", _Usage(10, 10), 10, True)
    inner = usage_mod.start_run("inner")
    usage_mod.record("b", _Usage(99, 99), 99, True)
    assert usage_mod.current().run_id == "inner"
    assert usage_mod.current().total("prompt_tokens") == 99
    usage_mod.end_run(inner)
    assert usage_mod.current().run_id == "outer"
    assert usage_mod.current().total("prompt_tokens") == 10
    usage_mod.end_run(outer)


# --------------------------------------------------------------------------- #
# 3. The free-tier profile is internally coherent
# --------------------------------------------------------------------------- #

def _profile() -> dict[str, str]:
    values = {}
    for line in (REPO_ROOT / ".env.free-tier").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_free_tier_profile_disables_thinking():
    """Thinking is what forced the large per-call budgets; without it the small
    budgets below become viable."""
    profile = _profile()
    assert profile["AGENT_REASONING_EFFORT"] == "none"
    assert profile["AGENT_EDIT_REASONING_EFFORT"] == "none"


def test_free_tier_profile_survives_the_startup_clamp():
    """max_file_chars is clamped to max_tokens * 3.5 * 0.75. A profile whose
    file cap is silently reduced at startup would not be the profile it claims
    to document."""
    profile = _profile()
    s = Settings(
        model=profile["AGENT_MODEL"],
        max_tokens=int(profile["AGENT_MAX_TOKENS"]),
        max_file_chars=int(profile["AGENT_MAX_FILE_CHARS"]),
        edit_reasoning_effort=profile["AGENT_EDIT_REASONING_EFFORT"],
    )
    assert s.max_file_chars == int(profile["AGENT_MAX_FILE_CHARS"])


def test_free_tier_profile_leaves_validation_to_detection():
    """Empty on purpose -- see test_env_example_does_not_pin_a_repo_specific_
    validate_command. What must hold is that validation is REACHABLE, not that a
    command is pinned: detection has to be able to produce one."""
    from agent import detect as detect_mod
    assert not _profile()["AGENT_VALIDATE_CMD"]
    assert callable(detect_mod.detect)


def test_free_tier_profile_raises_transient_patience():
    """429s are routine at this budget; the default patience is not enough."""
    assert int(_profile()["AGENT_MAX_TRANSIENT_RETRIES"]) >= 5


def test_workflow_serialises_runs_repo_wide():
    """Rate limits are enforced per organisation, so per-issue concurrency does
    not protect the shared token bucket: two PRs commented on at once means two
    runs draining one TPM allowance and both failing."""
    wf = (REPO_ROOT / ".github/workflows/comment-pr-agent.yml").read_text()
    assert "group: comment-pr-agent-${{ github.repository }}" in wf


def test_diff_cap_is_configurable():
    """Was a hardcoded 8000-char slice -- about a third of a free-tier minute."""
    assert Settings(max_diff_chars=2000).max_diff_chars == 2000
