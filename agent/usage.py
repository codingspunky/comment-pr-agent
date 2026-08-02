"""Per-run token accounting.

Why this exists: the whole reason `propose_changes` runs with thinking disabled
is that reasoning tokens are charged against the same completion ceiling as the
answer. That was an argument, not a measurement. This module makes it a
measurement -- `reasoning_tokens` per call is the number that says whether the
design is buying anything.

Two secondary uses:
  - free-tier survival. Groq charges prompt + REQUESTED max_completion_tokens
    against TPM, so `requested` is logged next to what was actually used: a large
    gap means you are paying rate-limit budget for headroom you never touched.
  - `cached_tokens`, because cached prefixes do not count toward TPM. If that
    number stays at zero, prompt restructuring is available and free.

State lives in a ContextVar, not a module global: the webhook runs agent jobs as
concurrent background tasks in one process, and a global accumulator would
silently blend two reviewers' runs together.
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("comment-pr-agent.usage")

_current: contextvars.ContextVar[Optional["RunUsage"]] = contextvars.ContextVar(
    "agent_run_usage", default=None)


@dataclass
class CallUsage:
    label: str
    thinking: bool
    requested: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def rate_limit_cost(self) -> int:
        """What this call cost against a TPM budget.

        Not the same as total_tokens. The provider counts the prompt plus the
        REQUESTED completion budget, and cached prefix tokens are excluded -- so a
        call that returns 200 tokens while requesting 12,000 still spends 12,000.
        """
        return max(0, self.prompt_tokens - self.cached_tokens) + self.requested


@dataclass
class RunUsage:
    run_id: str
    calls: list[CallUsage] = field(default_factory=list)

    def total(self, attr: str) -> int:
        return sum(getattr(c, attr) for c in self.calls)

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "llm_calls": len(self.calls),
            "prompt_tokens": self.total("prompt_tokens"),
            "completion_tokens": self.total("completion_tokens"),
            "reasoning_tokens": self.total("reasoning_tokens"),
            "cached_tokens": self.total("cached_tokens"),
            "rate_limit_cost": self.total("rate_limit_cost"),
        }


def start_run(run_id: str) -> contextvars.Token:
    """Begin accounting. Returns a token for `end_run`."""
    return _current.set(RunUsage(run_id=run_id))


def current() -> Optional[RunUsage]:
    return _current.get()


def end_run(token: contextvars.Token, settings=None) -> dict:
    """Log and return the run summary, then restore the previous context."""
    run = _current.get()
    summary = run.summary() if run else {}
    if run and run.calls:
        cost = estimate_cost(run, settings) if settings is not None else None
        if cost is not None:
            summary["estimated_cost_usd"] = round(cost, 5)
        log.info("run usage: %s", summary)
    _current.reset(token)
    return summary


def _get(obj, *names, default=0):
    """Read the first attribute (or dict key) that exists.

    Provider SDKs move these around between versions and nest some of them, so a
    missing field must degrade to 0 rather than raise -- accounting is
    observability, and observability must never be able to fail a run.
    """
    for name in names:
        if obj is None:
            return default
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name)
        if value is not None:
            return value
    return default


def record(label: str, usage, requested: int, thinking: bool) -> Optional[CallUsage]:
    """Record one completion's usage against the current run, if any.

    Wrapped in a broad except on purpose: this is instrumentation. A shape change
    in the provider's usage object must not be able to break an agent run.
    """
    run = _current.get()
    if run is None:
        return None
    try:
        completion_details = _get(usage, "completion_tokens_details", default=None)
        prompt_details = _get(usage, "prompt_tokens_details", default=None)
        call = CallUsage(
            label=label,
            thinking=thinking,
            requested=requested,
            prompt_tokens=int(_get(usage, "prompt_tokens", "input_tokens")),
            completion_tokens=int(_get(usage, "completion_tokens", "output_tokens")),
            reasoning_tokens=int(_get(completion_details, "reasoning_tokens")),
            cached_tokens=int(_get(prompt_details, "cached_tokens")),
        )
    except Exception:                              # pragma: no cover - defensive
        log.debug("could not parse usage for %s", label, exc_info=True)
        return None

    run.calls.append(call)
    log.info(
        "llm call %s: thinking=%s prompt=%d completion=%d reasoning=%d "
        "cached=%d requested=%d tpm_cost=%d",
        label, thinking, call.prompt_tokens, call.completion_tokens,
        call.reasoning_tokens, call.cached_tokens, call.requested,
        call.rate_limit_cost,
    )
    return call


def estimate_cost(run: RunUsage, settings) -> Optional[float]:
    """Estimated USD, or None when prices are not configured.

    Prices are NOT hardcoded. Provider rates change, and a stale table baked into
    source reports confident wrong numbers -- worse than reporting nothing. Set
    AGENT_PRICE_INPUT_PER_M / AGENT_PRICE_OUTPUT_PER_M from the provider's current
    pricing page to turn this on.
    """
    inp = getattr(settings, "price_input_per_m", 0.0)
    out = getattr(settings, "price_output_per_m", 0.0)
    if not inp and not out:
        return None
    return (run.total("prompt_tokens") / 1_000_000) * inp + \
           (run.total("completion_tokens") / 1_000_000) * out
