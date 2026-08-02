"""Tests for the hardening fixes: safe target reading, tool-JSON salvage,
truncation detection, base URL normalisation, token redaction, portable
validation, dedupe, config coherence."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.commands import read_targets
from agent.config import Settings
from agent.git_ops import _redact
from agent.llm import (
    TruncatedCompletion,
    next_max_tokens,
    salvage_tool_json,
    unwrap_salvaged,
    valid_edits,
    valid_plan,
)
from agent.sandbox import validate
from agent.webhook import _is_duplicate_delivery

# --------------------------------------------------------------------------- #
# read_targets: LLM-supplied paths are untrusted
# --------------------------------------------------------------------------- #

def _repo(tmp_path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return tmp_path


def test_read_targets_reads_and_flags_new_files(tmp_path):
    _repo(tmp_path)
    contents, skipped = read_targets(str(tmp_path), ["app/main.py", "app/new_module.py"])
    assert contents["app/main.py"].startswith("print")
    assert contents["app/new_module.py"] == ""          # to be created
    assert skipped == []


def test_read_targets_blocks_traversal_and_absolute(tmp_path):
    _repo(tmp_path)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    contents, skipped = read_targets(str(tmp_path), ["../secret.txt", str(outside)])
    assert contents == {}
    assert len(skipped) == 2
    assert all("outside repository" in s for s in skipped)


def test_read_targets_blocks_git_directory(tmp_path):
    """`.git/config` can carry the credential. Shipping it into a prompt is how
    a token ends up echoed back into a public PR body."""
    _repo(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        "[remote]\n url = https://x-access-token:ghs_SECRET@github.com/o/r.git\n",
        encoding="utf-8")
    contents, skipped = read_targets(str(tmp_path), [".git/config", "app/main.py"])
    assert ".git/config" not in contents
    assert "ghs_SECRET" not in "".join(contents.values())
    assert any("protected" in s for s in skipped)


def test_read_targets_skips_oversized_instead_of_truncating(tmp_path):
    _repo(tmp_path)
    (tmp_path / "big.py").write_text("x" * 1000, encoding="utf-8")
    contents, skipped = read_targets(str(tmp_path), ["big.py"], max_chars=100)
    assert "big.py" not in contents                     # never truncated
    assert any("too large" in s for s in skipped)


def test_read_targets_size_cap_counts_characters_not_bytes(tmp_path):
    """A file of multi-byte characters is under the CHARACTER cap it is
    measured against, even though its byte count is not."""
    _repo(tmp_path)
    (tmp_path / "uni.py").write_text("é" * 100, encoding="utf-8")   # 200 bytes, 100 chars
    contents, skipped = read_targets(str(tmp_path), ["uni.py"], max_chars=150)
    assert "uni.py" in contents and skipped == []


def test_read_targets_enforces_file_count_cap(tmp_path):
    _repo(tmp_path)
    targets = [f"f{i}.py" for i in range(12)]
    contents, skipped = read_targets(str(tmp_path), targets, max_files=8)
    assert len(contents) == 8
    assert sum("file limit" in s for s in skipped) == 4


def test_read_targets_zero_files_reads_nothing(tmp_path):
    """max_files=0 used to mean 'unlimited' -- the slice `[:max(0,n) or None]`
    turned 0 into None. It also reported every file as both read and skipped."""
    _repo(tmp_path)
    contents, skipped = read_targets(str(tmp_path), ["app/main.py"], max_files=0)
    assert contents == {}
    assert len(skipped) == 1


def test_read_targets_skips_directories(tmp_path):
    _repo(tmp_path)
    contents, skipped = read_targets(str(tmp_path), ["app"])
    assert contents == {}
    assert any("directory" in s for s in skipped)


# --------------------------------------------------------------------------- #
# Groq hardening
# --------------------------------------------------------------------------- #

def test_salvage_parses_wrapped_generation():
    failed = ('<function=submit_changes>'
              '{"edits": [], "summary": "s", "pr_title": "t", "pr_body": "b"}'
              '</function>')
    assert salvage_tool_json(
        failed) == {"edits": [], "summary": "s", "pr_title": "t", "pr_body": "b"}


def test_salvage_returns_none_on_broken_json():
    assert salvage_tool_json('<function=x>{"a": "unclosed}</function>') is None
    assert salvage_tool_json("no braces at all") is None


def test_unwrap_salvaged_extracts_our_tool_args():
    wrapped = {"name": "record_plan", "arguments": {"target_files": ["a.py"], "approach": "do it"}}
    assert unwrap_salvaged(
        wrapped, ["record_plan"]) == {"target_files": ["a.py"], "approach": "do it"}


def test_unwrap_salvaged_rejects_hallucinated_tools():
    wrapped = {"name": "repo_browser.open_file", "arguments": {"path": "a.py"}}
    assert unwrap_salvaged(wrapped, ["record_plan"]) is None


def test_unwrap_salvaged_passes_plain_args_through():
    plain = {"target_files": ["a.py"], "approach": "do it"}
    assert unwrap_salvaged(plain, ["record_plan"]) == plain


def test_base_url_normalisation():
    s = Settings(groq_api_key="k", groq_base_url="https://api.groq.com/openai/v1")
    from agent.llm import _client
    client = _client(s)
    assert "openai/v1/openai/v1" not in str(client.base_url)


def test_next_max_tokens_halves_with_floor():
    assert next_max_tokens(8000) == 4000
    assert next_max_tokens(4000) == 2000
    assert next_max_tokens(2000) == 1500          # floor
    assert next_max_tokens(1500) == 1500          # no progress -> caller raises


def test_valid_plan_rejects_empty_plans():
    assert not valid_plan({})
    assert not valid_plan({"target_files": [], "approach": "x"})
    assert not valid_plan({"target_files": ["a.py"], "approach": "  "})
    assert valid_plan({"target_files": ["a.py"], "approach": "do it"})


def test_valid_edits_rejects_empty_and_malformed():
    assert not valid_edits({"edits": []})
    assert not valid_edits({"edits": [{"action": "write"}]})
    assert valid_edits({"edits": [{"path": "a.py", "action": "write", "content": "x"}]})


# --------------------------------------------------------------------------- #
# Truncation -- the failure mode that silently destroys files
# --------------------------------------------------------------------------- #

class _FakeChoice:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason
        self.message = object()


class _FakeResponse:
    def __init__(self, finish_reason):
        self.choices = [_FakeChoice(finish_reason)]


def test_truncated_completion_is_rejected(monkeypatch):
    """`submit_changes` carries complete file bodies. A completion cut off at
    the token ceiling is a half-written file; using it overwrites the
    reviewer's code with a fragment."""
    from agent import llm

    class _FakeCompletions:
        def create(self, **kwargs):
            return _FakeResponse("length")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm, "_client", lambda settings: _FakeClient())
    s = Settings(groq_api_key="k")
    with pytest.raises(TruncatedCompletion):
        llm._create_forced(s, [{"role": "user", "content": "x"}],
                           tools=[llm.EDITS_TOOL], tool_choice="required", max_tokens=100)


def test_complete_completion_is_accepted(monkeypatch):
    from agent import llm

    sentinel = _FakeResponse("stop")

    class _FakeCompletions:
        def create(self, **kwargs):
            return sentinel

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm, "_client", lambda settings: _FakeClient())
    s = Settings(groq_api_key="k")
    out = llm._create_forced(s, [{"role": "user", "content": "x"}],
                             tools=[llm.EDITS_TOOL], tool_choice="required", max_tokens=100)
    assert out is sentinel.choices[0].message


# --------------------------------------------------------------------------- #
# Config coherence
# --------------------------------------------------------------------------- #

def test_max_file_chars_clamped_to_output_budget():
    """A file that survives the read cap must be re-emittable in full. The old
    defaults let a 48k-char file in against a ~14k-char completion budget."""
    s = Settings(max_tokens=4000, max_file_chars=48_000)
    assert s.max_file_chars <= int(4000 * 3.5 * 0.75)


def test_placeholder_values_are_treated_as_unset(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "replace-with-a-long-random-string")
    s = Settings()
    assert s.github_webhook_secret == ""
    with pytest.raises(RuntimeError, match="placeholder"):
        s.require("github_webhook_secret")


# --------------------------------------------------------------------------- #
# Token redaction
# --------------------------------------------------------------------------- #

def test_git_error_redacts_url_token():
    line = "fatal: unable to access 'https://x-access-token:ghs_abc123@github.com/o/r.git/'"
    assert "ghs_abc123" not in _redact(line)


def test_git_error_redacts_auth_header_and_raw_token():
    import base64
    token = "ghs_supersecrettoken"
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    text = f"AUTHORIZATION: basic {basic}\nand raw {token} here"
    out = _redact(text, token)
    assert token not in out and basic not in out


# --------------------------------------------------------------------------- #
# Validation sandbox
# --------------------------------------------------------------------------- #

def test_validate_timeout_is_a_result_not_a_crash(tmp_path):
    s = Settings(validate_cmd="python -c \"import time; time.sleep(5)\"", validate_timeout=1)
    result = validate(str(tmp_path), s)
    assert result.ran and not result.ok and "timed out" in result.output


def test_detected_python_command_uses_and_not_semicolon(tmp_path):
    """With `;` a failed install is swallowed and the tests run against a stale
    environment, reporting green for the wrong reason."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    from agent.sandbox import _detect_command
    # isolated_validate=False: this test is about the shell operator, and
    # building a real virtualenv to assert on a string would make it slow and
    # leave temp directories behind.
    cmd = _detect_command(str(tmp_path), Settings(isolated_validate=False))
    assert "&&" in cmd and ";" not in cmd and ">/dev/null" not in cmd


def test_repo_validate_cmd_is_opt_in(tmp_path):
    """.agent.yml is repo-supplied shell. Off unless the operator opts in."""
    (tmp_path / ".agent.yml").write_text("validate: echo pwned\n", encoding="utf-8")
    from agent.sandbox import _detect_command
    assert _detect_command(str(tmp_path), Settings(allow_repo_validate_cmd=False)) is None
    assert _detect_command(str(tmp_path), Settings(allow_repo_validate_cmd=True)) == "echo pwned"


def test_validation_output_is_redacted(tmp_path):
    s = Settings(groq_api_key="gsk_supersecretkey",
                 validate_cmd="python -c \"print('leaked gsk_supersecretkey')\" && exit 1")
    result = validate(str(tmp_path), s)
    assert "gsk_supersecretkey" not in result.output


# --------------------------------------------------------------------------- #
# Webhook dedupe
# --------------------------------------------------------------------------- #

def test_duplicate_delivery_dedupe():
    assert not _is_duplicate_delivery("delivery-abc")
    assert _is_duplicate_delivery("delivery-abc")
    assert not _is_duplicate_delivery("")          # missing header is not a dupe
