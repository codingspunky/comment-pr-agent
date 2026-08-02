"""The shipped Actions workflow is configuration, and configuration rots silently.

Every assertion here corresponds to something that was actually wrong, or that
would break a real deployment without any test noticing:

  - the install step pointed at `github.com/YOUR-ORG/comment-pr-agent`, a repo
    that does not exist, so the workflow could never have run as shipped;
  - it carried none of the free-tier token settings, so on a free Groq key the
    edit call requested 12,000 completion tokens against an 8,000 TPM ceiling --
    a request that can never be admitted, at any backoff;
  - it set AGENT_ALLOW_REPO_VALIDATE_CMD=true, which makes a repo skill's
    command override detection. The aiops skill's command does not install
    pytest, and a fresh runner has none, so validation would fail with
    ModuleNotFoundError on a change that was actually fine.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" \
    / "comment-pr-agent.yml"


@pytest.fixture(scope="module")
def wf():
    if not WORKFLOW.exists():
        pytest.skip("workflow not present in this layout")
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def run_step(wf):
    for step in wf["jobs"]["agent"]["steps"]:
        if step.get("name") == "Run the agent":
            return step
    pytest.fail("no 'Run the agent' step")


def _code_lines(run: str) -> str:
    """The run block minus comments -- asserting on prose is not a check."""
    return "\n".join(ln for ln in run.splitlines() if not ln.strip().startswith("#"))


def test_workflow_is_valid_yaml(wf):
    assert wf["name"] == "comment-pr-agent"


def test_triggers_on_comment_creation(wf):
    # `on` is the YAML 1.1 boolean True unless quoted -- a real footgun here.
    on = wf.get("on") or wf.get(True)
    assert on["issue_comment"]["types"] == ["created"]


def test_permissions_are_the_minimum_needed(wf):
    perms = wf["permissions"]
    assert perms == {"contents": "write", "pull-requests": "write"}


def test_runs_are_serialised_across_the_repository(wf):
    """Per-issue grouping would let two runs drain one TPM bucket at once."""
    assert "github.event.issue.number" not in wf["concurrency"]["group"]
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_author_association_is_gated_before_a_runner_starts(wf):
    cond = wf["jobs"]["agent"]["if"]
    for role in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert role in cond
    assert "CONTRIBUTOR" not in cond.replace("COLLABORATOR", "")
    assert "!= 'Bot'" in cond


def test_python_authorization_gate_is_also_applied(run_step):
    """Two gates, deliberately: the YAML `if` and the Python re-check."""
    code = _code_lines(run_step["run"])
    assert "--enforce-authorization" in code
    assert "--author-association" in code


def test_comment_body_never_reaches_a_shell(wf, run_step):
    """A comment containing $(...) must not execute on the runner."""
    code = _code_lines(run_step["run"])
    assert "--comment-from-env" in code
    assert "github.event.comment.body" not in code
    assert run_step["env"]["AGENT_COMMENT"] == "${{ github.event.comment.body }}"

    gate = next(s for s in wf["jobs"]["agent"]["steps"]
                if s.get("name") == "Check the trigger phrase")
    assert "github.event.comment.body" not in gate["run"]


def test_checkout_does_not_persist_credentials(wf):
    checkout = next(s for s in wf["jobs"]["agent"]["steps"]
                    if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False


def test_install_source_has_no_placeholder_org(wf):
    """The shipped v0.6.0 pointed at a nonexistent repo and nothing caught it."""
    pkg = wf["env"]["AGENT_PACKAGE"]
    assert "YOUR-ORG" not in pkg, "stale placeholder"
    # YOUR-USERNAME is the documented thing the operator replaces; the point of
    # this test is that it is a single, greppable location.
    assert pkg.count("YOUR-USERNAME") <= 1


def test_install_step_uses_the_package_variable(wf):
    step = next(s for s in wf["jobs"]["agent"]["steps"]
                if s.get("name") == "Install the agent")
    code = _code_lines(step["run"])
    assert "$AGENT_PACKAGE" in code
    assert "git+https" not in code, "hardcoded URL bypasses the env var"


# --------------------------------------------------------------------------- #
# The free-tier budget: the reason the shipped workflow could not work
# --------------------------------------------------------------------------- #

FREE_TIER_TPM = 8000


def test_edit_budget_fits_a_free_tier_ceiling(run_step):
    """Groq charges prompt + REQUESTED completion, used or not."""
    requested = int(run_step["env"]["AGENT_MAX_TOKENS"])
    typical_prompt = 2400          # observed on a real run, with headroom
    assert requested + typical_prompt < FREE_TIER_TPM, (
        f"edit call would cost ~{requested + typical_prompt} against a "
        f"{FREE_TIER_TPM} ceiling and could never be admitted")


@pytest.mark.parametrize("key,ceiling", [
    ("AGENT_INTERPRET_MAX_TOKENS", 2000),
    ("AGENT_PLAN_MAX_TOKENS", 2000),
    ("AGENT_MAX_TOKENS", 4000),
])
def test_every_call_budget_is_set_and_bounded(run_step, key, ceiling):
    assert key in run_step["env"], f"{key} unset; the default will not fit"
    assert int(run_step["env"][key]) <= ceiling


def test_reasoning_is_off_on_the_edit_call(run_step):
    """Reasoning tokens spent here come out of the file-body budget."""
    assert run_step["env"]["AGENT_EDIT_REASONING_EFFORT"] == "none"


def test_file_cap_is_not_below_an_ordinary_source_file(run_step):
    """4000 refused a 4,053-character module outright. Do not regress to it."""
    assert int(run_step["env"]["AGENT_MAX_FILE_CHARS"]) >= 6000


# --------------------------------------------------------------------------- #
# Validation strategy
# --------------------------------------------------------------------------- #

def test_auto_detection_is_enabled(run_step):
    """Empty AGENT_VALIDATE_CMD is what turns detection on."""
    assert run_step["env"]["AGENT_VALIDATE_CMD"] == ""


def test_repo_supplied_validate_cmd_is_not_trusted_by_default(run_step):
    """A skill command runs at the repo root, outside the isolated venv.

    The aiops skill's command does not install pytest; a fresh runner has none.
    Detection installs it. Preferring detection is the safer default.
    """
    assert run_step["env"]["AGENT_ALLOW_REPO_VALIDATE_CMD"] == "false"


def test_isolated_validation_is_on(run_step):
    assert run_step["env"]["AGENT_ISOLATED_VALIDATE"] == "true"


def test_dotenv_search_is_disabled(run_step):
    """The cwd is the target repo; a committed .env could redirect our tokens."""
    assert run_step["env"]["AGENT_NO_DOTENV"] == "true"


def test_secrets_come_from_secrets_not_literals(run_step):
    for key in ("GROQ_API_KEY", "GITHUB_TOKEN"):
        assert run_step["env"][key].startswith("${{ secrets."), \
            f"{key} must come from a repository secret"


def test_console_script_is_used_not_module_invocation(run_step):
    """`python -m agent.run` in the target repo's cwd would let a repo-local
    `agent/` package shadow ours."""
    code = _code_lines(run_step["run"])
    assert "python -m agent.run" not in code
    assert code.strip().startswith("comment-pr-agent")
    assert code.strip().splitlines()[-1].strip().endswith('"$GITHUB_WORKSPACE"')


def test_existing_checkout_is_reused(run_step):
    assert "--workspace" in _code_lines(run_step["run"])
