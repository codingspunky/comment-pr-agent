"""Pure, dependency-free logic.

No regex, no template matching, no brittle keyword parsing. The only string
checks here are `is_addressed` (a configurable addressing convention, so we
don't invoke the LLM on every unrelated comment) and the protected-path
denylist. All interpretation of *what to do* is done by the model
(see agent/llm.py), not by patterns here.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal, Optional

Mode = Literal["existing", "new"]

# Paths the model may never read or write, even though they live inside the
# repository root. Containment alone is not enough:
#   .git/              — `config` can hold the credential; `hooks/*` execute on
#                        the agent host at `git commit`, i.e. OUTSIDE the
#                        sandboxed validation step the README tells you to
#                        isolate.
#   .github/workflows/ — runs in CI with the repository's secrets.
#   .agent/            — the agent's own skills. An edit landing here means the
#                        agent rewrites the instructions governing its next run.
PROTECTED_PREFIXES: tuple[str, ...] = (
    ".git",
    ".github/workflows",
    ".agent",
)


def decide_mode(pr_number: Optional[int], wants_new_pr: bool) -> Mode:
    """The router. Inputs are facts, not parsed patterns:

    - `pr_number` is a structural fact from the webhook payload (is the comment
      on an open PR?).
    - `wants_new_pr` is the LLM's read of the reviewer's intent.

    Logic: explicit new-PR intent OR no PR present -> open a new PR; otherwise
    work the existing PR's branch.
    """
    if wants_new_pr:
        return "new"
    if pr_number is not None:
        return "existing"
    return "new"


def is_addressed(body: str, trigger: str, require_trigger: bool = True) -> bool:
    """Cheap addressing gate (NOT intent parsing).

    When `require_trigger` is False, every comment is routed to the LLM, which
    decides whether it's a request for the agent. When True, the comment must
    mention the configurable trigger handle.

    The trigger must start a line and be followed by whitespace or end-of-line.
    A bare substring check fires on quoted replies ("> /agent do the thing"),
    on fenced code blocks containing the trigger, and on longer words that
    merely start with it (`/agentic`).
    """
    if not require_trigger:
        return True
    if not body or not trigger:
        return False
    trigger_l = trigger.lower()
    in_fence = False
    for raw_line in body.lower().splitlines():
        line = raw_line.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(">"):          # quoted text from another comment
            continue
        if not line.startswith(trigger_l):
            continue
        rest = line[len(trigger_l):]
        if rest == "" or rest[0].isspace():
            return True
    return False


def verify_signature(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature (X-Hub-Signature-256).

    An empty secret is rejected outright. Without this, an unset
    GITHUB_WEBHOOK_SECRET means every request is HMAC'd with an empty key --
    which anyone can reproduce, so the endpoint would authenticate the world.
    """
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, signature_header)


# GitHub's fixed enum of author associations considered trusted by default.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def is_authorized(author_association: Optional[str], author_login: Optional[str],
                  allowlist: Optional[set[str]] = None) -> bool:
    allowlist = allowlist or set()
    if author_login and author_login in allowlist:
        return True
    return (author_association or "").upper() in TRUSTED_ASSOCIATIONS


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #

def normalise_rel(rel: str) -> str:
    """Normalise a model-supplied repository-relative path for comparison.

    Backslashes become forward slashes (models emit Windows separators) and a
    leading `./` is dropped. `..` segments AND a leading `/` are kept intact:
    stripping the slash would turn `/etc/passwd` into a path inside the repo
    instead of the escape attempt it actually is, and the containment check
    below needs to see it as absolute to reject it.
    """
    text = str(rel).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    if text.startswith("/"):
        return "/" + text.lstrip("/")
    return text


def is_protected_path(rel: str) -> bool:
    """True if `rel` falls under a PROTECTED_PREFIXES entry."""
    norm = normalise_rel(rel).lstrip("/")
    if not norm:
        return False
    parts = PurePosixPath(norm).parts
    for prefix in PROTECTED_PREFIXES:
        pparts = PurePosixPath(prefix).parts
        if parts[: len(pparts)] == pparts:
            return True
    return False


def resolve_within(repo_path: str, rel: str) -> Optional[Path]:
    """Resolve `rel` against the repo root, returning None if it escapes.

    `.resolve()` collapses `..` and follows symlinks, so a symlinked file
    pointing outside the tree is caught here too.
    """
    norm = normalise_rel(rel)
    if not norm:
        return None
    # Absolute (POSIX) or drive-qualified (Windows) paths are escapes, not
    # relative paths that happen to start with a separator.
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        return None
    root = Path(repo_path).resolve()
    target = (root / norm).resolve()
    if target == root:
        return None
    if root not in target.parents:
        return None
    return target


@dataclass
class Edit:
    path: str
    content: str
    action: Literal["write", "delete"] = "write"


def edits_from_args(edits_arg: list) -> list[Edit]:
    """Convert already-structured tool-call arguments into Edit objects.

    Input is structured data returned by the model's function call (parsed JSON
    from the tool-call arguments) -- there is no free-text parsing here.
    """
    edits: list[Edit] = []
    for e in edits_arg or []:
        if not isinstance(e, dict):
            raise ValueError(f"edit entry is not an object: {e!r}")
        action = e.get("action", "write")
        if action not in ("write", "delete"):
            raise ValueError(f"unknown action: {action}")
        path = e.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("edit entry is missing a path")
        edits.append(Edit(path=path, content=e.get("content") or "", action=action))
    return edits


def apply_edits(repo_path: str, edits: list[Edit]) -> list[str]:
    """Apply edits to a working tree, all-or-nothing.

    Refuses to escape the repo root AND refuses to touch PROTECTED_PREFIXES.
    Returns the repository-relative paths that were written or deleted.

    Every path is checked BEFORE anything is written. Validating and writing in
    one pass meant a rejected fourth edit left the first three on disk: the
    caller sees an exception and assumes nothing happened, while the tree has
    been half-modified. Under GitHub Actions that tree is the job's checkout,
    which the runner does not clean up between steps.
    """
    planned: list[tuple[Edit, str, Path]] = []
    for edit in edits:
        rel = normalise_rel(edit.path)
        if is_protected_path(rel):
            raise ValueError(f"edit path is protected and may not be modified: {edit.path}")
        target = resolve_within(repo_path, rel)
        if target is None:
            raise ValueError(f"edit path escapes repo root: {edit.path}")
        planned.append((edit, rel, target))

    touched: list[str] = []
    for edit, rel, target in planned:
        if edit.action == "delete":
            if target.exists():
                target.unlink()
            touched.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit.content, encoding="utf-8")
        touched.append(rel)
    return touched


def read_targets(repo_path: str, target_files: list[str],
                 max_files: int = 8,
                 max_chars: int = 48_000) -> tuple[dict[str, str], list[str]]:
    """Safely read LLM-chosen target files from the working tree.

    Returns (contents, skipped). Guards:
      - containment: a path resolving outside the repo root is SKIPPED;
      - protection: a path under PROTECTED_PREFIXES is SKIPPED -- `.git/config`
        can carry the credential, and shipping it into a prompt is how a token
        ends up echoed back into a public PR body;
      - per-file size cap: an oversized file is skipped entirely rather than
        truncated -- sending a truncated file for a "complete rewrite" would
        destroy the part the model never saw. The cap is measured in
        CHARACTERS, matching the prompt budget it protects; `st_size` is only
        a cheap pre-filter (a UTF-8 byte count is always >= the char count);
      - file-count cap: keeps one edit call inside the token budget.
    Missing files are legitimate (the model may be creating them): included
    with empty content.
    """
    contents: dict[str, str] = {}
    skipped: list[str] = []

    limit = max(0, max_files)
    requested = [str(p) for p in (target_files or [])]
    head, overflow = requested[:limit], requested[limit:]

    for raw in head:
        rel = normalise_rel(raw)
        if not rel:
            continue
        if is_protected_path(rel):
            skipped.append(f"{rel} (protected path)")
            continue
        target = resolve_within(repo_path, rel)
        if target is None:
            skipped.append(f"{rel} (outside repository)")
            continue
        if target.is_dir():
            skipped.append(f"{rel} (is a directory)")
            continue
        if not target.exists():
            contents[rel] = ""            # new file to be created
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
            if len(text) > max_chars:
                skipped.append(f"{rel} (too large: {len(text)} chars > {max_chars} cap)")
                continue
            contents[rel] = text
        except OSError as exc:
            skipped.append(f"{rel} (unreadable: {exc})")

    skipped.extend(f"{normalise_rel(p)} (over the {limit}-file limit)" for p in overflow)
    return contents, skipped


def slugify(text: str, max_len: int = 32) -> str:
    """Branch-name-safe slug, built without regex.

    Lowercase ASCII alphanumerics are kept; every other run collapses into a
    single dash. Non-ASCII is dropped rather than passed through: git accepts
    UTF-8 refs but plenty of tooling downstream does not.
    """
    out: list[str] = []
    prev_dash = False
    for ch in text.lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")[:max_len].strip("-")
    return slug or "change"


def load_allowlist_from_env() -> set[str]:
    raw = os.environ.get("AGENT_AUTHOR_ALLOWLIST", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace occurrences of the given secret values with ***.

    Applied to validation output before it is posted to a PR comment: the
    repo's own test suite is arbitrary code and may echo its environment.
    """
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    return out
