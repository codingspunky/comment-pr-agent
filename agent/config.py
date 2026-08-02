"""Configuration, loaded from the environment.

No secrets are hardcoded. See .env.example for the full list.

On import, an OPERATOR-OWNED .env file (if present) is loaded into the
environment, so the CLI and the webhook both pick it up automatically. Real
environment variables always win over .env values.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

# --------------------------------------------------------------------------- #
# .env loading
#
# SECURITY: never search upward from the current working directory. Under the
# GitHub Actions deployment the cwd is $GITHUB_WORKSPACE -- the TARGET
# repository -- so a cwd-relative search loads a .env committed by whoever
# opened the pull request. `override=False` protects the variables the workflow
# sets explicitly (GITHUB_TOKEN, GROQ_API_KEY, AGENT_ALLOW_REPO_VALIDATE_CMD)
# and NOTHING ELSE, so a repo-committed .env could:
#   - point GROQ_BASE_URL or GITHUB_API_BASE at its own host, which then
#     receives the Authorization header carrying a live credential;
#   - set AGENT_VALIDATE_CMD to an arbitrary shell command, bypassing the
#     AGENT_ALLOW_REPO_VALIDATE_CMD gate entirely (that gate only covers repo
#     SKILLS and .agent.yml).
#
# So we load only from paths the operator controls:
#   1. $AGENT_ENV_FILE, if set -- explicit always wins;
#   2. a .env next to the installed `agent` package, i.e. the project root of a
#      source checkout, which is how the local and webhook deployments run.
# Set AGENT_NO_DOTENV=1 to skip entirely (containers should get real env vars).
# --------------------------------------------------------------------------- #

_TRUTHY = {"1", "true", "yes", "on"}


def _dotenv_candidates() -> List[Path]:
    explicit = os.environ.get("AGENT_ENV_FILE", "").strip()
    if explicit:
        return [Path(explicit)]
    # <checkout>/agent/config.py -> <checkout>/.env
    return [Path(__file__).resolve().parent.parent / ".env"]


def load_env_file() -> Optional[str]:
    """Load an operator-owned .env. Returns the path loaded, or None.

    Deliberately NOT `find_dotenv(usecwd=True)`: see the note above.
    """
    if os.environ.get("AGENT_NO_DOTENV", "").strip().lower() in _TRUTHY:
        return None
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    for candidate in _dotenv_candidates():
        try:
            if candidate.is_file():
                load_dotenv(candidate, override=False)   # real env vars still win
                return str(candidate)
        except OSError:
            continue
    return None


LOADED_ENV_FILE = load_env_file()


# Values shipped in .env.example. `setup.sh` copies that file to .env, so a
# deployment that forgets to edit it would otherwise run with a secret that is
# published in the repository. Treated as absent.
PLACEHOLDER_VALUES = {
    "replace-with-a-long-random-string",
    "gsk_...",
    "123456",
    "",
}

# Rough characters-per-token for the models we target. Used to check that the
# input cap and the output budget are coherent: the edit call asks for the
# COMPLETE new contents of every file it was shown, so a file that fits the
# read cap must also fit the completion budget or it gets truncated on the way
# back out and written to disk half-finished.
CHARS_PER_TOKEN = 3.5

# Per-model hard ceilings, as published by the provider. `max_output_tokens` is
# the one that matters here: requesting more is a guaranteed 400, and requesting
# close to it on a REASONING model is a guaranteed truncation, because thinking
# tokens are billed against the same completion budget as the answer.
#
#   qwen/qwen3.6-27b   131,072 context / 16,384 output   (Groq, reasoning+tools)
#   openai/gpt-oss-120b 131,072 context /  32,766 output
#
# Unknown models fall back to DEFAULT_MODEL_LIMITS rather than being clamped to
# something arbitrary -- an unrecognised model still runs, it just does not get
# a provider-specific safety net.
MODEL_LIMITS: dict[str, dict[str, int]] = {
    "qwen/qwen3.6-27b": {"context": 131_072, "max_output_tokens": 16_384},
    "qwen/qwen3-32b": {"context": 131_072, "max_output_tokens": 40_960},
    "openai/gpt-oss-120b": {"context": 131_072, "max_output_tokens": 32_766},
    "openai/gpt-oss-20b": {"context": 131_072, "max_output_tokens": 32_766},
}
DEFAULT_MODEL_LIMITS = {"context": 131_072, "max_output_tokens": 16_384}

# Valid values for the Groq `reasoning_effort` parameter. "" means "omit the
# parameter entirely", for a model or gateway that rejects it.
REASONING_EFFORTS = {"", "none", "low", "medium", "high", "default"}

# Fraction of the completion budget available for the edit call's file bodies.
# The rest covers summary / pr_title / pr_body and the JSON envelope -- and, when
# the edit call runs WITH thinking enabled, the reasoning trace too. Thinking is
# not free here: it shares the ceiling with the file contents it is reasoning
# about, so the reserve roughly doubles.
EDIT_BUDGET_FRACTION_NO_THINKING = 0.75
EDIT_BUDGET_FRACTION_THINKING = 0.45


def model_limits(model: str) -> dict[str, int]:
    return MODEL_LIMITS.get(model.strip(), DEFAULT_MODEL_LIMITS)


def _effort(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip().lower()
    return value if value in REASONING_EFFORTS else default


def _read_private_key() -> str:
    """Return the GitHub App private key PEM.

    Accepts either a raw PEM in GITHUB_APP_PRIVATE_KEY, a base64-encoded PEM in
    GITHUB_APP_PRIVATE_KEY_B64, or a path in GITHUB_APP_PRIVATE_KEY_PATH.
    """
    if os.environ.get("GITHUB_APP_PRIVATE_KEY"):
        return os.environ["GITHUB_APP_PRIVATE_KEY"]
    if os.environ.get("GITHUB_APP_PRIVATE_KEY_B64"):
        return base64.b64decode(os.environ["GITHUB_APP_PRIVATE_KEY_B64"]).decode("utf-8")
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    return ""


def _clean(name: str, default: str = "") -> str:
    """Read an env var, treating documented placeholder values as unset."""
    value = os.environ.get(name, default).strip()
    return "" if value in PLACEHOLDER_VALUES else value


@dataclass
class Settings:
    # GitHub App auth (webhook / CLI deployments)
    github_app_id: str = field(default_factory=lambda: _clean("GITHUB_APP_ID"))
    github_app_private_key: str = field(default_factory=_read_private_key)
    github_webhook_secret: str = field(default_factory=lambda: _clean("GITHUB_WEBHOOK_SECRET"))
    github_api_base: str = field(
        default_factory=lambda: os.environ.get("GITHUB_API_BASE", "https://api.github.com"))
    # Pre-minted token (GitHub Actions supplies one; skips App auth entirely).
    github_token: str = field(default_factory=lambda: _clean("GITHUB_TOKEN"))

    # LLM (Groq -- OpenAI-compatible, ultra-fast inference)
    groq_api_key: str = field(default_factory=lambda: _clean("GROQ_API_KEY"))
    groq_base_url: str = field(
        default_factory=lambda: os.environ.get("GROQ_BASE_URL", "https://api.groq.com"))
    model: str = field(default_factory=lambda: os.environ.get("AGENT_MODEL", "qwen/qwen3.6-27b"))
    max_tokens: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_MAX_TOKENS", "12000")))
    # 0.6 is the provider's recommendation for PRECISE CODING in thinking mode
    # (the general thinking-mode default of 1.0 is for open-ended reasoning).
    # The old 0.1 predates this model: near-greedy sampling makes Qwen's thinking
    # mode loop on itself, which burns the completion budget before the answer.
    temperature: float = field(
        default_factory=lambda: float(os.environ.get("AGENT_TEMPERATURE", "0.6")))
    top_p: float = field(default_factory=lambda: float(os.environ.get("AGENT_TOP_P", "0.95")))
    request_timeout: float = field(
        default_factory=lambda: float(os.environ.get("AGENT_LLM_TIMEOUT", "120")))

    # ---- Reasoning (thinking) mode -------------------------------------- #
    # qwen3.6-27b is dual-mode. Thinking tokens are charged against the SAME
    # completion ceiling as the answer, so the mode is set per call, not globally:
    #
    #   interpret / plan -> thinking ON. Tiny outputs (a routing decision, a file
    #       list), so there is room, and the judgement is the whole point.
    #   edit             -> thinking OFF. The output is complete file bodies and
    #       `allow_shrink=False` means this call cannot shrink its budget. A
    #       reasoning trace here competes directly with the file contents and
    #       makes TruncatedCompletion -- the one failure that can half-write a
    #       reviewer's file -- substantially more likely.
    reasoning_effort: str = field(
        default_factory=lambda: _effort("AGENT_REASONING_EFFORT", "default"))
    edit_reasoning_effort: str = field(
        default_factory=lambda: _effort("AGENT_EDIT_REASONING_EFFORT", "none"))
    # "hidden" keeps the trace out of the response entirely. "parsed" puts it in a
    # separate field. Never "raw": that leaves <think> blocks in message.content,
    # which is where tool arguments are parsed from.
    reasoning_format: str = field(
        default_factory=lambda: os.environ.get("AGENT_REASONING_FORMAT", "hidden").strip().lower())

    # ---- Per-call completion budgets ------------------------------------ #
    # These were hardcoded at 1000 / 2000 in llm.py, which is fine for a
    # non-reasoning model and fatal for a reasoning one: the thinking trace alone
    # overruns 1000 tokens on most comments, the completion stops at the ceiling,
    # and the truncation guard correctly rejects it -- so EVERY run failed at the
    # interpret step. They are settings now so they scale with the model.
    interpret_max_tokens: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_INTERPRET_MAX_TOKENS", "4000")))
    plan_max_tokens: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_PLAN_MAX_TOKENS", "6000")))

    # Behaviour
    trigger_phrase: str = field(default_factory=lambda: os.environ.get("AGENT_TRIGGER", "/agent"))
    require_trigger: bool = field(
        default_factory=lambda: os.environ.get("AGENT_REQUIRE_TRIGGER", "true").lower() == "true")
    max_retries: int = field(default_factory=lambda: int(os.environ.get("AGENT_MAX_RETRIES", "3")))
    # Retries for transient provider errors (429/5xx), separate from validation
    # retries. On a small TPM budget one run costs more than a minute's
    # allowance, so a 429 mid-run is the normal path and this is what lets a run
    # finish rather than fail.
    max_transient_retries: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_MAX_TRANSIENT_RETRIES", "5")))

    # Context budget guards. max_file_chars is clamped in __post_init__ so a
    # file that survives the read cap can always be re-emitted in full.
    max_target_files: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_MAX_TARGET_FILES", "8")))
    max_file_chars: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_MAX_FILE_CHARS", "20000")))
    # The PR diff shown to the planner. Was a hardcoded 8000-char slice, which is
    # ~2,300 tokens -- a third of a free-tier minute spent on context the planner
    # barely uses on a small repo.
    max_diff_chars: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_MAX_DIFF_CHARS", "8000")))

    default_reviewers: list[str] = field(
        default_factory=lambda: [r.strip() for r in
                                 os.environ.get(
                                     "AGENT_DEFAULT_REVIEWERS", "").split(",") if r.strip()])
    git_author_name: str = field(
        default_factory=lambda: os.environ.get("AGENT_GIT_NAME", "comment-agent[bot]"))
    git_author_email: str = field(
        default_factory=lambda: os.environ.get(
            "AGENT_GIT_EMAIL", "comment-agent[bot]@users.noreply.github.com"))

    # Validation sandbox
    validate_cmd: str = field(default_factory=lambda: os.environ.get("AGENT_VALIDATE_CMD", ""))
    validate_timeout: int = field(
        default_factory=lambda: int(os.environ.get("AGENT_VALIDATE_TIMEOUT", "900")))
    skip_validation_if_unknown: bool = field(
        default_factory=lambda: os.environ.get(
            "AGENT_SKIP_VALIDATION_IF_UNKNOWN", "true").lower() == "true")
    # Allow a repo-committed skill to supply the shell command that runs on the
    # agent host. Safe on an ephemeral runner (GitHub Actions); on a long-lived
    # host it turns "can commit to a branch" into "can run code where the App
    # private key lives", so it defaults OFF.
    allow_repo_validate_cmd: bool = field(
        default_factory=lambda: os.environ.get(
            "AGENT_ALLOW_REPO_VALIDATE_CMD", "false").lower() == "true")

    # Detected Python validation installs the target repo's dependencies. Doing
    # that in the agent's own environment lets a repo's pins collide with groq
    # or langgraph and break the agent itself, and pip runs the repo's build
    # backend either way. A throwaway virtualenv contains both.
    isolated_validate: bool = field(
        default_factory=lambda: os.environ.get(
            "AGENT_ISOLATED_VALIDATE", "true").strip().lower() in _TRUTHY)
    # Building the venv is separate from running the suite: a slow dependency
    # install should not eat the test suite's budget.
    validate_setup_timeout: float = field(
        default_factory=lambda: float(os.environ.get("AGENT_VALIDATE_SETUP_TIMEOUT", "300")))

    # Optional cost estimation. Unset by default: provider prices change, and a
    # stale table compiled into source reports confident wrong numbers, which is
    # worse than reporting none. Fill these from the provider's pricing page.
    price_input_per_m: float = field(
        default_factory=lambda: float(os.environ.get("AGENT_PRICE_INPUT_PER_M", "0") or 0))
    price_output_per_m: float = field(
        default_factory=lambda: float(os.environ.get("AGENT_PRICE_OUTPUT_PER_M", "0") or 0))

    # Persistence (LangGraph checkpointer). If unset, an in-memory saver is used.
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))

    # Where working trees are cloned.
    workdir: str = field(
        default_factory=lambda: os.environ.get("AGENT_WORKDIR", "/tmp/comment-pr-agent"))

    def __post_init__(self) -> None:
        # 1. Never request more output than the model can produce. Groq rejects
        #    the request outright (400), and the old code had no idea what the
        #    ceiling was -- AGENT_MAX_TOKENS=32000 was silently fine on
        #    gpt-oss-120b and a hard failure on qwen3.6-27b, whose ceiling is
        #    16,384. Clamping beats a 400 at the worst possible moment.
        cap = model_limits(self.model)["max_output_tokens"]
        if self.max_tokens > cap:
            self.max_tokens = cap
        for name in ("interpret_max_tokens", "plan_max_tokens"):
            if getattr(self, name) > cap:
                setattr(self, name, cap)

        # 2. The edit call must be able to emit every file it was shown, in full.
        #    How much of the ceiling that leaves depends on whether the edit call
        #    is thinking: a reasoning trace spends the same budget as the answer.
        fraction = (EDIT_BUDGET_FRACTION_THINKING
                    if self.edit_reasoning_effort not in ("", "none")
                    else EDIT_BUDGET_FRACTION_NO_THINKING)
        budget_chars = int(self.max_tokens * CHARS_PER_TOKEN * fraction)
        if self.max_file_chars > budget_chars:
            self.max_file_chars = max(2_000, budget_chars)

    # ---- Provider request shaping ---------------------------------------- #

    def completion_kwargs(self, max_tokens: int, thinking: bool = True) -> dict:
        """Sampling + budget parameters for one chat.completions call.

        `max_completion_tokens` rather than the deprecated `max_tokens`: on a
        reasoning model the two are not synonyms -- the former is documented to
        cover reasoning AND visible output, which is exactly the accounting this
        agent needs to respect.
        """
        kwargs: dict = {
            "max_completion_tokens": max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        effort = self.reasoning_effort if thinking else self.edit_reasoning_effort
        if effort:
            kwargs["reasoning_effort"] = effort
        if self.reasoning_format:
            kwargs["reasoning_format"] = self.reasoning_format
        return kwargs

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                f"missing required settings: {', '.join(missing)} "
                f"(unset, empty, or still the .env.example placeholder)")

    def secret_values(self) -> list[str]:
        """Secrets to scrub from anything echoed back to GitHub."""
        return [v for v in (self.groq_api_key, self.github_webhook_secret,
                            self.github_token, self.github_app_private_key) if v]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop the cached Settings so env changes take effect."""
    get_settings.cache_clear()
