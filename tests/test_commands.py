"""Pure-logic tests: addressing, routing, auth, signatures, edits, paths."""
from __future__ import annotations

import pytest

from agent.commands import (
    Edit,
    apply_edits,
    decide_mode,
    edits_from_args,
    is_addressed,
    is_authorized,
    is_protected_path,
    normalise_rel,
    redact_secrets,
    slugify,
    verify_signature,
)

# --------------------------------------------------------------------------- #
# Addressing
# --------------------------------------------------------------------------- #

def test_addressed_requires_trigger_when_enabled():
    assert is_addressed("/agent fix the bug", "/agent", require_trigger=True)
    assert not is_addressed("just a normal comment", "/agent", require_trigger=True)


def test_addressed_passes_everything_when_disabled():
    assert is_addressed("anything at all", "/agent", require_trigger=False)
    assert is_addressed("", "/agent", require_trigger=False)


def test_addressed_empty_body():
    assert not is_addressed("", "/agent", require_trigger=True)


def test_addressed_ignores_quoted_replies():
    """Quoting someone else's /agent comment must not re-trigger the agent."""
    body = "> /agent make the retry backoff exponential\n\nI agree with this."
    assert not is_addressed(body, "/agent", require_trigger=True)


def test_addressed_ignores_fenced_code_blocks():
    body = "Here's how you'd call it:\n```\n/agent do the thing\n```\nThoughts?"
    assert not is_addressed(body, "/agent", require_trigger=True)


def test_addressed_requires_word_boundary():
    """`/agentic` is not `/agent`."""
    assert not is_addressed("/agentic workflows are neat", "/agent", require_trigger=True)
    assert is_addressed("/agent do it", "/agent", require_trigger=True)
    assert is_addressed("/agent", "/agent", require_trigger=True)


def test_addressed_requires_line_start():
    assert not is_addressed("please ask /agent about it", "/agent", require_trigger=True)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def test_mode_comment_on_pr_works_existing():
    assert decide_mode(42, False) == "existing"


def test_mode_no_pr_opens_new():
    assert decide_mode(None, False) == "new"


def test_mode_intent_forces_new_even_with_pr():
    assert decide_mode(42, True) == "new"


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #

def test_authorized_by_association():
    assert is_authorized("COLLABORATOR", "someone")
    assert not is_authorized("NONE", "stranger")


def test_authorized_by_allowlist():
    assert is_authorized("NONE", "trusted", {"trusted"})


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #

def test_signature_roundtrip():
    import hashlib
    import hmac
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret-value", body, sig)


def test_signature_rejects_tampered_body():
    import hashlib
    import hmac
    sig = "sha256=" + hmac.new(b"s3cret-value", b'{"a":1}', hashlib.sha256).hexdigest()
    assert not verify_signature("s3cret-value", b'{"a":2}', sig)


def test_signature_rejects_missing_header():
    assert not verify_signature("s3cret-value", b"x", None)
    assert not verify_signature("s3cret-value", b"x", "md5=deadbeef")


def test_signature_rejects_empty_secret():
    """An unset secret must never authenticate anything.

    With an empty key the HMAC is reproducible by anyone, so accepting it means
    the endpoint authenticates the entire internet.
    """
    import hashlib
    import hmac
    body = b'{"a":1}'
    forged = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    assert not verify_signature("", body, forged)


# --------------------------------------------------------------------------- #
# Edit parsing
# --------------------------------------------------------------------------- #

def test_edits_from_args_basic():
    edits = edits_from_args([{"path": "a.py", "action": "write", "content": "x"}])
    assert edits[0].path == "a.py" and edits[0].content == "x"


def test_edits_from_args_delete_without_content():
    edits = edits_from_args([{"path": "a.py", "action": "delete"}])
    assert edits[0].action == "delete" and edits[0].content == ""


def test_edits_from_args_rejects_bad_action():
    with pytest.raises(ValueError):
        edits_from_args([{"path": "a.py", "action": "chmod"}])


def test_edits_from_args_rejects_missing_path():
    with pytest.raises(ValueError):
        edits_from_args([{"action": "write", "content": "x"}])


# --------------------------------------------------------------------------- #
# Applying edits
# --------------------------------------------------------------------------- #

def test_apply_edits_writes_and_deletes(tmp_path):
    (tmp_path / "old.py").write_text("gone", encoding="utf-8")
    touched = apply_edits(str(tmp_path), [
        Edit(path="new/dir/a.py", content="hello"),
        Edit(path="old.py", content="", action="delete"),
    ])
    assert (tmp_path / "new" / "dir" / "a.py").read_text() == "hello"
    assert not (tmp_path / "old.py").exists()
    assert set(touched) == {"new/dir/a.py", "old.py"}


def test_apply_edits_blocks_traversal(tmp_path):
    for bad in ["../escape.py", "/etc/passwd", "a/../../escape.py"]:
        with pytest.raises(ValueError, match="escapes repo root"):
            apply_edits(str(tmp_path), [Edit(path=bad, content="x")])


# --------------------------------------------------------------------------- #
# Protected paths -- the containment check alone was not enough
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    ".git/config",
    ".git/hooks/pre-commit",
    ".github/workflows/deploy.yml",
    ".agent/skills/repo-conventions/SKILL.md",
    "./.git/config",
    ".git\\hooks\\pre-commit",
])
def test_apply_edits_blocks_protected_paths(tmp_path, path):
    """`.git/hooks/*` executes on the agent host at commit time -- i.e. outside
    the sandboxed validation step. `.github/workflows/*` runs in CI with the
    repo's secrets. `.agent/*` lets the agent rewrite its own instructions."""
    with pytest.raises(ValueError, match="protected"):
        apply_edits(str(tmp_path), [Edit(path=path, content="#!/bin/sh\nid\n")])
    assert not (tmp_path / ".git").exists()


def test_apply_edits_allows_normal_github_files(tmp_path):
    """Only .github/workflows/ is protected, not all of .github/."""
    touched = apply_edits(str(tmp_path), [Edit(path=".github/ISSUE_TEMPLATE.md", content="hi")])
    assert touched == [".github/ISSUE_TEMPLATE.md"]


def test_is_protected_path_does_not_over_match():
    assert not is_protected_path("gitignore")
    assert not is_protected_path("src/.gitkeep")
    assert not is_protected_path("agentic/thing.py")
    assert is_protected_path(".git")
    assert is_protected_path(".agent/skills/x/SKILL.md")


def test_normalise_rel():
    assert normalise_rel("./a/b.py") == "a/b.py"
    assert normalise_rel("a\\b.py") == "a/b.py"
    # A leading slash is PRESERVED: stripping it would turn an escape attempt
    # into a valid in-repo path, which is how "/etc/passwd" would have become
    # "<repo>/etc/passwd" instead of being rejected.
    assert normalise_rel("  /a/b.py  ") == "/a/b.py"


def test_absolute_paths_are_rejected_not_relativised(tmp_path):
    from agent.commands import resolve_within
    assert resolve_within(str(tmp_path), "/etc/passwd") is None
    assert resolve_within(str(tmp_path), "C:/Windows/system32") is None
    assert resolve_within(str(tmp_path), "app/main.py") is not None


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #

def test_slugify():
    assert slugify("Make the Retry Backoff Exponential!") == "make-the-retry-backoff-exponenti"
    assert slugify("!!!") == "change"


def test_slugify_drops_non_ascii():
    """git accepts UTF-8 refs; plenty of downstream tooling does not."""
    assert slugify("修复这个问题") == "change"
    assert slugify("fix 修复 bug") == "fix-bug"


def test_redact_secrets():
    text = "token=gsk_supersecretvalue rest"
    assert "gsk_supersecretvalue" not in redact_secrets(text, ["gsk_supersecretvalue"])
    # too short to be a meaningful secret; not worth mangling unrelated output
    assert redact_secrets("abc", ["abc"]) == "abc"
