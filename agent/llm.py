"""LLM reasoning via Groq function calling, with progressive skill disclosure
and hardened tool-call handling.

Calls:
  interpret_comment() -> {is_request, wants_new_pr, instruction}
  plan_change()       -> {target_files, approach} + may load skills first
  propose_changes()   -> {edits, summary, pr_title, pr_body}

Hardening:
  - base URL normalisation: groq>=1.x appends /openai/v1 itself; a legacy base
    URL carrying that suffix is stripped so both forms work;
  - every Groq call goes through _create_forced(), which handles Groq's
    `tool_use_failed` 400 (models fumble JSON quote-escaping when code rides
    inside string arguments): salvage the rejected generation if it is
    actually parseable, otherwise retry with repair feedback -- bounded;
  - TRUNCATION: a completion that stops because it hit the token ceiling is
    rejected, never used. `submit_changes` carries the COMPLETE new contents
    of each file, so a truncated response is a half-written file. This is the
    one failure mode that silently destroys a reviewer's code, so it fails
    loudly instead;
  - transient 429/5xx get bounded exponential backoff;
  - REASONING MODE is set per call, not globally. On a dual-mode model
    (qwen/qwen3.6-27b) thinking tokens are charged against the same completion
    ceiling as the answer, so `interpret` and `plan` think (small outputs, real
    judgement) while `propose_changes` does not (its output is complete file
    bodies and it is forbidden from shrinking its budget). Unknown reasoning
    parameters are dropped rather than fatal, so an OpenAI-compatible gateway
    that does not implement them still works.
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

import groq
from groq import Groq

from . import usage as usage_mod
from .config import Settings, get_settings


class TruncatedCompletion(RuntimeError):
    """The model hit the output ceiling mid-answer. The result is unusable."""


class PromptTooLarge(RuntimeError):
    """The request exceeded the provider's per-request / TPM limit and could
    not be shrunk any further.

    Distinct from TruncatedCompletion: nothing was generated at all. Raised so
    the graph can reply with a useful message instead of an internal error.
    """


class NoChangesNeeded(RuntimeError):
    """The model read the code and concluded the request is already satisfied.

    This is a CORRECT outcome, not a failure. A reviewer asking for something
    that already exists is ordinary, and the previous behaviour -- a bare
    RuntimeError from `propose_changes` -- turned it into an unhandled
    exception with a stack trace, which is both alarming and wrong.

    It subclasses RuntimeError so any caller that was already catching the old
    error keeps working; callers that care about the distinction catch this
    first. `reason` is the model's own explanation, to be relayed to the
    reviewer verbatim.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Tool (function) schemas -- the structure lives here, not in a prompt template.
# --------------------------------------------------------------------------- #

INTERPRET_TOOL = {
    "type": "function",
    "function": {
        "name": "record_request",
        "description": (
            "Record your interpretation of a reviewer's comment left on a GitHub pull request "
            "or issue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "is_request": {
                    "type": "boolean",
                    "description": (
                        "True only if the comment is asking the coding agent to make a concrete"
                        " code change. False for general discussion, questions, or chatter."
                    ),
                },
                "wants_new_pr": {
                    "type": "boolean",
                    "description": (
                        "True if the reviewer wants the change delivered as a brand-new, "
                        "separate pull request, rather than committed onto the pull request the"
                        " comment is on. Infer from natural language (e.g. 'in a separate PR', "
                        "'open a new PR for this'). If the comment is not on a pull request, "
                        "this is irrelevant."
                    ),
                },
                "instruction": {
                    "type": "string",
                    "description": (
                        "A clear, self-contained restatement of the change to make, suitable "
                        "for another engineer to act on."
                    ),
                },
            },
            "required": ["is_request", "wants_new_pr", "instruction"],
        },
    },
}

PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "record_plan",
        "description": "Record the plan for a focused code change.",
        "parameters": {
            "type": "object",
            "properties": {
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Repository-relative paths of files to modify or create. Prefer the "
                        "minimal set."
                    ),
                },
                "approach": {
                    "type": "string",
                    "description": "Two to four sentences describing how the change will be made.",
                },
            },
            "required": ["target_files", "approach"],
        },
    },
}

LOAD_SKILLS_TOOL = {
    "type": "function",
    "function": {
        "name": "load_skills",
        "description": (
            "Load the full procedure of one or more skills from the skill index before "
            "planning. Only load skills clearly relevant to this change."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill names exactly as they appear in the skill index.",
                },
            },
            "required": ["names"],
        },
    },
}

EDITS_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_changes",
        "description": "Submit the complete set of file changes for this request.",
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "One entry per file changed.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string", "description": "Repository-relative file path."},
                            "action": {"type": "string", "enum": ["write", "delete"]},
                            "content": {
                                "type": "string",
                                "description": "Complete new file contents. Omit for delete."},
                        },
                        "required": ["path", "action"],
                    },
                },
                "no_changes_needed": {
                    "type": "boolean",
                    "description": "True if the request is ALREADY satisfied by the code you "
                                   "were shown, so no edit is required. Send an empty `edits` "
                                   "array and explain why in `summary`. Do not invent a "
                                   "cosmetic change just to have something to submit."},
                "summary": {
                    "type": "string", "description": "One sentence describing what changed."},
                "pr_title": {"type": "string", "description": "Concise pull request title."},
                "pr_body": {"type": "string", "description": "Markdown pull request description."},
            },
            "required": ["edits", "summary", "pr_title", "pr_body"],
        },
    },
}


# --------------------------------------------------------------------------- #
# Hardened Groq call core
# --------------------------------------------------------------------------- #

def _client(settings: Settings) -> Groq:
    settings.require("groq_api_key")
    # groq>=1.x appends /openai/v1/... itself; strip a legacy suffix so an
    # old-style base URL in someone's .env can't double the path (404).
    base = settings.groq_base_url.rstrip("/")
    if base.endswith("/openai/v1"):
        base = base[: -len("/openai/v1")]
    return Groq(api_key=settings.groq_api_key, base_url=base,
                timeout=settings.request_timeout)


def _tool_use_failure(err: groq.BadRequestError) -> Optional[str]:
    """Return the rejected generation if this is a tool_use_failed 400, else None."""
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        e = body.get("error") or {}
        if e.get("code") == "tool_use_failed":
            return str(e.get("failed_generation") or "")
    return None


def salvage_tool_json(failed: str) -> Optional[dict]:
    """Cheap salvage before retrying: sometimes the arguments are parseable
    once the <function=...> wrapper is stripped."""
    start, end = failed.find("{"), failed.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(failed[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def unwrap_salvaged(salvaged: dict, allowed_names: List[str]) -> Optional[dict]:
    """A salvaged dict shaped {"name": ..., "arguments": {...}} is a TOOL CALL,
    not tool arguments. Some models hallucinate tools from their training (e.g.
    `repo_browser.open_file`) when they want a capability we did not give them.
    If the call targets one of our tools, return its arguments; if it targets a
    foreign tool, reject (None) so the caller retries with corrective feedback
    instead of mistaking the call envelope for a plan."""
    keys = set(salvaged.keys())
    if "name" in keys and "arguments" in keys and keys <= {"name", "arguments", "id", "type"}:
        if salvaged["name"] in allowed_names and isinstance(salvaged["arguments"], dict):
            return salvaged["arguments"]
        return None
    return salvaged


_JSON_REPAIR_NOTE = (
    "Your previous function call was REJECTED because its arguments were not "
    "valid JSON. The usual cause is unescaped double quotes or raw newlines "
    "inside string values -- e.g. quotes in code or docstrings placed in the "
    "`content` field. Call the function again with the SAME substance, but "
    "make the arguments strictly valid JSON: every double quote inside a "
    "string value must be \\\" and every newline must be \\n. For Python "
    "docstrings, prefer '''single-quoted''' triple quotes so there is nothing "
    "to escape.\n\nYour rejected output, for reference:\n"
)

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

# Ceiling on a server-suggested wait. A hostile or malformed header must not be
# able to park a run for an hour; 75s covers a full rolling-minute TPM window
# with margin.
MAX_SERVER_WAIT = 75.0


def _retry_after_seconds(err) -> Optional[float]:
    """Seconds the provider asked us to wait, from the response headers.

    Groq publishes `retry-after` plus `x-ratelimit-reset-*`. Reading them is not
    a nicety on a small TPM budget: one run of this agent costs more tokens than
    a free-tier minute allows, so a 429 mid-run is the NORMAL path, not the
    exceptional one -- and the fixed backoff below tops out at 8s against a
    60-second window. That combination gave up on requests that would have
    succeeded, roughly 45 seconds early.

    Values arrive as either seconds ("30") or a duration ("7.66s"). Anything
    unparseable returns None, so the caller falls back to exponential backoff.
    """
    resp = getattr(err, "response", None)
    headers = getattr(resp, "headers", None) or {}
    if not hasattr(headers, "get"):
        return None
    for key in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = headers.get(key)
        if not raw:
            continue
        text = str(raw).strip().lower()
        # Durations can be "1m2.5s"; take the minutes and seconds we understand.
        try:
            if text.endswith("ms"):
                return float(text[:-2]) / 1000.0
            if "m" in text and text.endswith("s"):
                mins, _, rest = text.partition("m")
                return float(mins) * 60 + float(rest.rstrip("s") or 0)
            return float(text.rstrip("s"))
        except ValueError:
            continue
    return None


def _sleep_backoff(attempt: int, err=None) -> None:      # pragma: no cover
    """Wait before a retry: the server's number if it gave one, else exponential."""
    wait = _retry_after_seconds(err) if err is not None else None
    if wait is None:
        wait = min(8.0, (2 ** attempt) * 0.5)
    time.sleep(min(wait, MAX_SERVER_WAIT) + random.random() * 0.25)


def _create(client, settings: Settings, model: str, messages: List[dict],
            tools: List[dict], tool_choice, max_tokens: int, thinking: bool):
    """One raw completion call, with a one-shot retry that drops parameters the
    installed SDK or gateway does not recognise.

    `reasoning_effort` / `reasoning_format` are reasoning-model parameters. A
    self-hosted OpenAI-compatible gateway (or an older groq SDK) can reject them
    with a TypeError before the request is even sent. Degrading to a plain call
    is strictly better than refusing to run: the agent still works, it just loses
    per-call thinking control.
    """
    kwargs = settings.completion_kwargs(max_tokens, thinking=thinking)
    try:
        return client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            tool_choice=tool_choice, **kwargs)
    except TypeError:
        for key in ("reasoning_effort", "reasoning_format", "top_p"):
            kwargs.pop(key, None)
        return client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            tool_choice=tool_choice, **kwargs)


def _create_forced(settings: Settings, messages: List[dict], tools: List[dict],
                   tool_choice, max_tokens: int,
                   max_json_retries: int = 2,
                   max_transient_retries: int | None = None,
                   allow_shrink: bool = True,
                   thinking: bool = True,
                   label: str = "llm"):
    """One chat.completions call with tool_use_failed salvage, bounded repair
    retries, transient-error backoff, and truncation detection.

    Returns either a parsed-args dict (salvage path) or the response message
    (normal path). Raises TruncatedCompletion if the model ran out of room, or
    PromptTooLarge if the request itself will not fit.

    THREE INDEPENDENT BUDGETS. They used to share one `for attempt in
    range(1 + max_json_retries)` loop, so three consecutive 429s -- routine on
    a free tier -- exhausted the loop and surfaced as
    `RuntimeError("exhausted tool-call retries")` instead of the actual rate
    limit, and a single 413 shrink silently spent one of the JSON repair
    attempts. Each budget is now counted separately, and each still terminates:
      - json_retries   bounded by max_json_retries
      - transient      bounded by max_transient_retries
      - 413 shrinking  bounded by next_max_tokens()'s floor (it stops making
                       progress, and we raise)

    `allow_shrink=False` is for calls whose output must be COMPLETE -- notably
    propose_changes, which emits whole file bodies. Halving the completion
    budget to squeeze under a TPM limit makes a truncated file more likely,
    which is the one failure that can overwrite a reviewer's code. Such callers
    get PromptTooLarge instead of a smaller, riskier request.
    """
    client = _client(settings)
    msgs = list(messages)
    if max_transient_retries is None:
        max_transient_retries = settings.max_transient_retries
    transient = 0
    json_retries = 0

    while True:
        try:
            resp = _create(client, settings, settings.model, msgs, tools,
                           tool_choice, max_tokens, thinking)
            choice = resp.choices[0]
            # THE important check. `submit_changes` carries complete file
            # bodies; a completion cut off at the ceiling yields a half-written
            # file that apply_edits would happily write over the reviewer's
            # code. Never salvage a truncated generation.
            if getattr(choice, "finish_reason", None) == "length":
                hint = ("raise AGENT_MAX_TOKENS or ask for a narrower change"
                        if not thinking else
                        "on a reasoning model the thinking trace shares this "
                        "ceiling with the answer, so either raise the budget or "
                        "set AGENT_REASONING_EFFORT=none for this call")
                raise TruncatedCompletion(
                    f"the model hit its {max_tokens}-token output ceiling before "
                    f"finishing. The change is too large to emit in one response; "
                    f"{hint}.")
            # Recorded AFTER the truncation check: a rejected generation still
            # cost tokens, but attributing it to a successful call would make the
            # per-call numbers lie. Truncated calls show up as the gap between
            # `llm_calls` and the node count.
            usage_mod.record(label, getattr(resp, "usage", None), max_tokens, thinking)
            return choice.message

        except groq.BadRequestError as err:
            failed = _tool_use_failure(err)
            if failed is None:
                raise                              # a different 400: real error
            allowed = [t["function"]["name"] for t in tools]
            salvaged = salvage_tool_json(failed)
            unwrapped = unwrap_salvaged(salvaged, allowed) if salvaged is not None else None
            if unwrapped is not None:
                return unwrapped
            if json_retries >= max_json_retries:
                raise
            json_retries += 1
            if salvaged is not None and unwrapped is None:
                note = (f"You attempted to call a tool that DOES NOT EXIST here. "
                        f"The only tools available are: {', '.join(allowed)}. "
                        "You cannot open, browse, or read files at this stage -- "
                        "decide using only the information already in this "
                        "conversation, then call one of the available tools.\n\n"
                        "Your rejected output:\n")
            else:
                note = _JSON_REPAIR_NOTE
            msgs = msgs + [{"role": "user", "content": note + failed[:4000]}]

        except groq.APIStatusError as err:
            status = getattr(err, "status_code", None)
            if status == 413:
                # Groq counts prompt + REQUESTED max_tokens against TPM, so a
                # large completion budget alone can exceed a free-tier minute.
                if not allow_shrink:
                    raise PromptTooLarge(
                        "the request exceeded the model's size limit and this call "
                        "must emit complete file contents, so the completion budget "
                        "cannot be reduced safely. Lower AGENT_MAX_FILE_CHARS or "
                        "AGENT_MAX_TARGET_FILES, or ask for a narrower change."
                    ) from err
                reduced = next_max_tokens(max_tokens)
                if reduced == max_tokens:
                    raise PromptTooLarge(
                        f"the request still exceeded the model's size limit at the "
                        f"minimum completion budget ({max_tokens} tokens). The prompt "
                        f"itself is too large -- lower AGENT_MAX_FILE_CHARS or "
                        f"AGENT_MAX_TARGET_FILES."
                    ) from err
                max_tokens = reduced
                continue
            if status in _RETRYABLE_STATUS and transient < max_transient_retries:
                transient += 1
                _sleep_backoff(transient, err)
                continue
            raise
        except (groq.APIConnectionError, groq.APITimeoutError) as err:
            if transient >= max_transient_retries:
                raise
            transient += 1
            _sleep_backoff(transient, err)
            continue


def _pick_call(msg, expected_name: str):
    """Some models return multiple tool calls in one message; prefer the one
    matching the expected name instead of blindly taking index 0."""
    for call in msg.tool_calls or []:
        if call.function.name == expected_name:
            return call
    return (msg.tool_calls or [None])[0]


def _args_from(result, expected_name: str) -> dict:
    """Normalise _create_forced output (salvaged dict or message) to args."""
    if isinstance(result, dict):
        return result
    call = _pick_call(result, expected_name)
    if call is None:
        raise RuntimeError(f"model did not call `{expected_name}`")
    return json.loads(call.function.arguments)


def next_max_tokens(current: int) -> int:
    """Backoff step for Groq 413s: TPM accounting counts prompt + REQUESTED
    max_tokens, so a large completion budget alone can exceed a free-tier
    minute. Halve, floored at 1500."""
    return max(1500, current // 2)


def valid_plan(args: dict) -> bool:
    """Some models emit record_plan with missing/empty arguments; an empty plan
    downstream means editing files the model never read."""
    files = args.get("target_files")
    approach = args.get("approach")
    return (isinstance(files, list) and len(files) > 0
            and all(isinstance(f, str) and f.strip() for f in files)
            and isinstance(approach, str) and bool(approach.strip()))


def valid_edits(args: dict) -> bool:
    """Reject an edit response that carries no actual file changes."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return False
    for e in edits:
        if not isinstance(e, dict):
            return False
        if not isinstance(e.get("path"), str) or not e["path"].strip():
            return False
    return True


# Deliberately narrow. This is the FALLBACK path for a model that ignores the
# `no_changes_needed` field, and the failure mode it must not have is treating a
# broken response as a considered decision -- a model that fails to produce
# edits will still happily write "Added the docstring" in `summary`, and reading
# that as "nothing to do" would silently swallow a real failure. So: explicit
# statements of non-action only, no bare "already".
_NO_CHANGE_RE = re.compile(
    r"\bno\s+(?:change|changes|edit|edits|modification|modifications)\s+"
    r"(?:are\s+|is\s+|were\s+|was\s+)?(?:needed|required|necessary)\b"
    r"|\bnothing\s+to\s+(?:do|change|fix)\b"
    r"|\balready\s+(?:present|exists?|satisfied|implemented|documented|correct)\b"
    r"|\balready\s+(?:has|have|contains?|includes?|provides?)\b",
    re.IGNORECASE,
)


def no_change_reason(args: dict) -> Optional[str]:
    """The model's stated reason for making no edits, or None.

    None means "this is a failure, treat it as one". A string means the model
    made a deliberate, explained decision that the work is already done.

    Two ways to qualify, with different strictness on purpose:

      1. `no_changes_needed: true` -- the explicit field. Trusted even if
         `edits` is absent entirely, because the model has stated its intent.
      2. Prose matching `_NO_CHANGE_RE` -- only when `edits` is an explicitly
         EMPTY list. A missing or malformed `edits` key is a broken response,
         and a broken response that happens to contain the right words must not
         be promoted into a decision.
    """
    if not isinstance(args, dict):
        return None
    edits = args.get("edits")
    if isinstance(edits, list) and edits:
        return None                      # there are edits; this is not a no-op

    text = " ".join(
        str(args.get(key) or "") for key in ("summary", "pr_body", "pr_title")
    ).strip()

    if args.get("no_changes_needed") is True:
        return text or "the requested change is already present in the code"

    if isinstance(edits, list) and text and _NO_CHANGE_RE.search(text):
        return text
    return None


def _call_tool(settings: Settings, system: str, user: str, tool: dict, max_tokens: int,
               allow_shrink: bool = True, thinking: bool = True,
               label: str = "llm") -> dict:
    """Force one function call and return its parsed arguments dict."""
    name = tool["function"]["name"]
    result = _create_forced(
        settings,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": name}},
        max_tokens=max_tokens,
        allow_shrink=allow_shrink,
        thinking=thinking,
        label=label,
    )
    return _args_from(result, name)


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #

def interpret_comment(comment: str, has_open_pr: bool, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    system = (
        "You read reviewer comments on GitHub and decide what the coding agent should do. "
        "Call record_request with your interpretation. Do not invent a task if the comment "
        "is not actually requesting a code change."
    )
    where = "an open pull request" if has_open_pr else "an issue (no pull request)"
    user = (
        f"The comment is on {where}.\n\n"
        f"Comment:\n{comment}\n"
    )
    return _call_tool(settings, system, user, INTERPRET_TOOL,
                      max_tokens=settings.interpret_max_tokens, label="interpret")


def plan_change(
    instruction: str,
    repo_files: List[str],
    diff: Optional[str],
    skills_index: str = "",
    always_skill_context: str = "",
    load_skill_bodies: Optional[Callable[[List[str]], str]] = None,
    settings: Settings | None = None,
    listing_truncated: bool = False,
) -> Tuple[dict, List[str], str]:
    """Plan the change, optionally loading skills first (one bounded round).

    Returns (plan_args, loaded_skill_names, loaded_skill_context).
    """
    settings = settings or get_settings()

    files_blob = "\n".join(repo_files)
    if listing_truncated:
        files_blob += ("\n[listing truncated -- the repository contains more files than "
                       "are shown. If the file you need is absent, say so in `approach` "
                       "and pick the closest listed file.]")

    system = (
        "You are a senior software engineer planning a minimal, focused code change. "
        "A skill index lists procedures you can load with load_skills; load the ones "
        "relevant to this change (or none), then call record_plan. "
        "You CANNOT open or read file contents at this stage and have no file-reading "
        "tools -- choose target_files from the repository file listing below; the full "
        "contents of your chosen files will be provided to you in the next step. "
        "For new functionality, pick the most plausible existing file (or name a "
        "sensible new one). "
        "You may not target anything under .git/, .github/workflows/, or .agent/."
    )
    if always_skill_context:
        # Repo-authored content is quoted as reference material inside the user
        # turn rather than promoted to the system role -- it is committed by
        # whoever can write to the repo and should not outrank our own
        # instructions.
        pass

    user = (
        f"Instruction:\n{instruction}\n\n"
        f"Skill index (loadable):\n{skills_index or '(none)'}\n\n"
    )
    if always_skill_context:
        user += (
            "Active procedures (reference material committed to the repository; "
            "follow them where they do not conflict with your instructions):\n"
            f"{always_skill_context}\n\n"
        )
    user += f"Repository files:\n{files_blob}\n"
    if diff:
        user += (f"\nDiff of the PR under review (truncated):\n"
                 f"{diff[:settings.max_diff_chars]}\n")

    messages: List[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    loaded_names: List[str] = []
    loaded_context = ""

    # Round 1: the model may load skills or plan directly.
    result = _create_forced(settings, messages, tools=[PLAN_TOOL, LOAD_SKILLS_TOOL],
                            tool_choice="required",
                            max_tokens=settings.plan_max_tokens, label="plan")

    if isinstance(result, dict):
        # Salvaged args -- we can't tell which tool without the wrapper; treat a
        # dict with 'names' as a load request, anything else as the plan.
        if "names" in result and "target_files" not in result:
            loaded_names = [
                str(n).strip().lower() for n in result.get("names", []) if str(n).strip()]
        else:
            if not valid_plan(result):
                result = _replan(settings, messages, result)
            return result, loaded_names, loaded_context
        call_id = "salvaged-load"
        raw_args = json.dumps(result)
    else:
        if not result.tool_calls:
            raise RuntimeError("model made no tool call in plan round 1")
        call = _pick_call(result, "load_skills")
        args = json.loads(call.function.arguments)
        if call.function.name == "record_plan":
            if not valid_plan(args):
                args = _replan(settings, messages, args)
            return args, loaded_names, loaded_context
        loaded_names = [str(n).strip().lower() for n in args.get("names", []) if str(n).strip()]
        call_id = call.id
        raw_args = call.function.arguments

    # load_skills path -- resolve, feed back, then FORCE record_plan (bounded).
    loaded_context = load_skill_bodies(
        loaded_names) if load_skill_bodies else "(no loader configured)"

    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "load_skills", "arguments": raw_args},
        }],
    })
    messages.append({"role": "tool", "tool_call_id": call_id, "content": loaded_context})

    result = _create_forced(settings, messages, tools=[PLAN_TOOL],
                            tool_choice={"type": "function", "function": {"name": "record_plan"}},
                            max_tokens=settings.plan_max_tokens, label="plan-after-skills")
    plan = _args_from(result, "record_plan")
    if not valid_plan(plan):
        plan = _replan(settings, messages, plan)
    return plan, loaded_names, loaded_context


def _replan(settings: Settings, messages: List[dict], bad: dict) -> dict:
    """One corrective retry when record_plan came back incomplete. An empty
    plan must never proceed: downstream it would mean rewriting files the model
    never read."""
    msgs = messages + [{
        "role": "user",
        "content": ("Your record_plan call was incomplete "
                    f"(received: {json.dumps(bad)[:500]}). You have no file-reading "
                    "tools; do not attempt to open files. Call record_plan again with "
                    "BOTH fields populated: target_files -- a non-empty array of "
                    "repository-relative file paths chosen from the listing -- and "
                    "approach -- 2 to 4 sentences describing the change."),
    }]
    result = _create_forced(settings, msgs, tools=[PLAN_TOOL],
                            tool_choice={"type": "function", "function": {"name": "record_plan"}},
                            max_tokens=settings.plan_max_tokens, label="replan")
    plan = _args_from(result, "record_plan")
    if not valid_plan(plan):
        raise RuntimeError(f"model could not produce a valid plan: {json.dumps(plan)[:300]}")
    return plan


def propose_changes(
    instruction: str,
    approach: str,
    file_contents: Dict[str, str],
    skill_context: str = "",
    constraints: str = "",
    feedback: Optional[str] = None,
    previous_attempt_diff: Optional[str] = None,
    settings: Settings | None = None,
) -> dict:
    """Produce the edits.

    `file_contents` is always the PRISTINE tree (the working tree is reset
    between retries), and `previous_attempt_diff` shows what the last attempt
    changed. Giving the model both means a retry is a correction rather than a
    blind re-derivation from the original file.
    """
    settings = settings or get_settings()
    files_section = "\n\n".join(
        f"=== FILE: {path} ===\n{content}" for path, content in file_contents.items()
    ) or "(no existing files provided; create new files as needed)"

    user = (
        f"Instruction:\n{instruction}\n\nApproach:\n{approach}\n\n"
    )
    if skill_context:
        user += (
            "Active procedures (reference material committed to the repository; "
            "follow them where they do not conflict with your instructions):\n"
            f"{skill_context}\n\n"
        )
    user += f"Current file contents:\n{files_section}\n"
    if constraints:
        user += f"\nConstraints:\n{constraints}\n"
    if previous_attempt_diff:
        user += (
            "\nYour PREVIOUS attempt produced this diff. It has been REVERTED --"
            " the file contents above are the pristine originals again, so submit"
            " complete contents as usual.\n"
            f"```diff\n{previous_attempt_diff}\n```\n"
        )
    if feedback:
        user += (
            f"\nThat attempt FAILED validation. Fix the root cause.\n"
            f"Validation output:\n{feedback[:6000]}\n"
        )

    system = (
        "You are a senior software engineer applying a focused change. Return the COMPLETE "
        "new contents of each file you change, preserving unrelated code exactly. Submit via "
        "the submit_changes function. "
        "You may not write to .git/, .github/workflows/, or .agent/ -- edits to those paths "
        "are rejected. "
        "Only submit files you were shown above or that you are creating; never rewrite a "
        "file whose current contents you have not seen. "
        "If the request is ALREADY satisfied by the code above, set no_changes_needed to true, "
        "send an empty edits array, and say why in summary -- do NOT invent a cosmetic change "
        "so that you have something to submit."
    )
    # allow_shrink=False: this call returns COMPLETE file bodies. Halving the
    # completion budget to fit under a TPM limit trades a clean failure for a
    # truncated file, and a truncated file is the one outcome that can destroy
    # the reviewer's code.
    # thinking=False: the edit budget belongs to file bodies. See
    # Settings.edit_reasoning_effort for why a reasoning trace here is the
    # single most dangerous place to spend completion tokens.
    args = _call_tool(settings, system, user, EDITS_TOOL,
                      max_tokens=settings.max_tokens, allow_shrink=False,
                      thinking=False, label="edit")
    if not valid_edits(args):
        # Order matters: "already done" is a correct answer and must be
        # separated from "the response was broken" BEFORE the generic raise.
        reason = no_change_reason(args)
        if reason is not None:
            raise NoChangesNeeded(reason)
        raise RuntimeError(
            "the model returned no usable file edits "
            f"(received: {json.dumps(args)[:300]})")
    return args
