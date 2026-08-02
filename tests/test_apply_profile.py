"""Tests for `scripts/apply_profile.py`.

The defect this script exists to prevent: `cp .env.free-tier .env` silently
overwrites the credentials you already filled in, because the profiles ship
their secret keys empty. Most of these tests are about that one failure.
"""
from __future__ import annotations

from pathlib import Path

from scripts.apply_profile import NEVER_OVERWRITE, merge, parse_env

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_parse_ignores_comments_and_blanks():
    parsed = parse_env("# a comment\n\nFOO=1\n  # indented comment\nBAR=2\n")
    assert parsed == {"FOO": "1", "BAR": "2"}


def test_parse_keeps_values_containing_spaces():
    """AGENT_VALIDATE_CMD is a shell command, not a token."""
    assert parse_env("AGENT_VALIDATE_CMD=python -m pytest -q\n") == {
        "AGENT_VALIDATE_CMD": "python -m pytest -q"}


# --------------------------------------------------------------------------- #
# The whole point: credentials survive
# --------------------------------------------------------------------------- #

def test_existing_secrets_are_never_blanked():
    """The profiles ship `GROQ_API_KEY=` empty. A naive copy wipes a real key and
    the next run fails with 'missing required settings' for no visible reason."""
    current = "GROQ_API_KEY=gsk_real_key\nGITHUB_APP_ID=12345\nAGENT_MAX_TOKENS=12000\n"
    profile = "GROQ_API_KEY=\nGITHUB_APP_ID=\nAGENT_MAX_TOKENS=3000\n"
    new, changed, added = merge(current, profile)
    values = parse_env(new)
    assert values["GROQ_API_KEY"] == "gsk_real_key"
    assert values["GITHUB_APP_ID"] == "12345"
    assert values["AGENT_MAX_TOKENS"] == "3000"
    assert changed == ["AGENT_MAX_TOKENS"]


def test_a_profile_cannot_overwrite_a_credential_even_if_it_sets_one():
    """A profile has no business asserting where your private key lives."""
    current = "GITHUB_APP_PRIVATE_KEY_PATH=/my/real/key.pem\n"
    profile = "GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/app-private-key.pem\n"
    new, changed, _ = merge(current, profile)
    assert parse_env(new)["GITHUB_APP_PRIVATE_KEY_PATH"] == "/my/real/key.pem"
    assert changed == []


def test_a_credential_you_do_not_have_yet_is_still_offered():
    """Protecting an existing value must not mean refusing to add a missing one."""
    new, _, added = merge("AGENT_MAX_TOKENS=12000\n",
                          "GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/key.pem\n")
    assert "GITHUB_APP_PRIVATE_KEY_PATH" in added
    assert parse_env(new)["GITHUB_APP_PRIVATE_KEY_PATH"] == "/run/secrets/key.pem"


# --------------------------------------------------------------------------- #
# Merge behaviour
# --------------------------------------------------------------------------- #

def test_an_empty_profile_value_clears_an_ordinary_setting():
    """Clearing is sometimes the point. AGENT_VALIDATE_CMD has to go back to
    empty for per-repo detection to apply, so a rule that refused to clear it
    would leave every existing install pinned to one repo's command."""
    new, changed, _ = merge("AGENT_VALIDATE_CMD=python -m pytest -q\n",
                            "AGENT_VALIDATE_CMD=\n")
    assert parse_env(new)["AGENT_VALIDATE_CMD"] == ""
    assert changed == ["AGENT_VALIDATE_CMD"]


def test_clearing_never_applies_to_a_credential():
    """The narrowed rule must not have widened the hole it was protecting."""
    new, changed, _ = merge("GROQ_API_KEY=gsk_real\n", "GROQ_API_KEY=\n")
    assert parse_env(new)["GROQ_API_KEY"] == "gsk_real"
    assert changed == []


def test_an_empty_profile_key_you_do_not_have_is_not_added_as_a_blank():
    new, changed, added = merge("AGENT_MAX_TOKENS=3000\n", "AGENT_DEFAULT_REVIEWERS=\n")
    assert not added and not changed


def test_profile_values_win_for_ordinary_settings():
    new, changed, _ = merge("AGENT_MAX_TOKENS=12000\n", "AGENT_MAX_TOKENS=3000\n")
    assert parse_env(new)["AGENT_MAX_TOKENS"] == "3000"
    assert changed == ["AGENT_MAX_TOKENS"]


def test_keys_absent_from_the_profile_are_untouched():
    new, _, _ = merge("AGENT_TRIGGER=/bot\nAGENT_MAX_TOKENS=12000\n",
                      "AGENT_MAX_TOKENS=3000\n")
    assert parse_env(new)["AGENT_TRIGGER"] == "/bot"


def test_new_keys_are_appended():
    """An .env written before v0.5.0 has none of the reasoning settings."""
    new, _, added = merge("AGENT_MAX_TOKENS=12000\n",
                          "AGENT_MAX_TOKENS=3000\nAGENT_REASONING_EFFORT=none\n")
    assert added == ["AGENT_REASONING_EFFORT"]
    assert parse_env(new)["AGENT_REASONING_EFFORT"] == "none"


def test_comments_and_ordering_survive():
    current = "# my notes\nAGENT_TRIGGER=/agent\n\n# budgets\nAGENT_MAX_TOKENS=12000\n"
    new, _, _ = merge(current, "AGENT_MAX_TOKENS=3000\n")
    assert "# my notes" in new
    assert "# budgets" in new
    assert new.index("AGENT_TRIGGER") < new.index("AGENT_MAX_TOKENS")


def test_no_changes_reports_nothing_to_do():
    new, changed, added = merge("AGENT_MAX_TOKENS=3000\n", "AGENT_MAX_TOKENS=3000\n")
    assert not changed and not added


def test_merge_is_idempotent():
    profile = "AGENT_MAX_TOKENS=3000\nAGENT_REASONING_EFFORT=none\n"
    once, _, _ = merge("AGENT_MAX_TOKENS=12000\n", profile)
    twice, changed, added = merge(once, profile)
    assert not changed and not added
    assert parse_env(twice) == parse_env(once)


# --------------------------------------------------------------------------- #
# Against the real profiles that ship
# --------------------------------------------------------------------------- #

def test_shipped_profiles_leave_every_secret_empty():
    """If a profile ever ships a non-empty credential, the NEVER_OVERWRITE guard
    is the only thing standing between it and someone's real key. Better that no
    profile contains one in the first place."""
    for name in (".env.example", ".env.free-tier"):
        values = parse_env((REPO_ROOT / name).read_text())
        for key in NEVER_OVERWRITE:
            assert not values.get(key), f"{name} ships a value for {key}"


def test_applying_the_free_tier_profile_to_a_filled_env_keeps_the_key():
    """End to end against the real file, with a realistic starting .env."""
    current = (REPO_ROOT / ".env.example").read_text().replace(
        "GROQ_API_KEY=", "GROQ_API_KEY=gsk_real_key").replace(
        "GITHUB_APP_ID=", "GITHUB_APP_ID=987654")
    profile = (REPO_ROOT / ".env.free-tier").read_text()
    new, changed, _ = merge(current, profile)
    values = parse_env(new)
    assert values["GROQ_API_KEY"] == "gsk_real_key"
    assert values["GITHUB_APP_ID"] == "987654"
    # and the profile actually took effect
    assert values["AGENT_MAX_TOKENS"] == "3000"
    assert values["AGENT_REASONING_EFFORT"] == "none"
    # Empty by design now; the merge must still carry the key across so an
    # older .env with a pinned command gets it cleared rather than kept.
    assert "AGENT_VALIDATE_CMD" in values
    assert "AGENT_MAX_TOKENS" in changed


def test_free_tier_profile_can_be_reversed_with_the_example_profile():
    """`bash apply-profile.sh example` must get you back to shipped defaults."""
    free = (REPO_ROOT / ".env.free-tier").read_text()
    example = (REPO_ROOT / ".env.example").read_text()
    back, _, _ = merge(free, example)
    assert parse_env(back)["AGENT_MAX_TOKENS"] == "12000"
    assert parse_env(back)["AGENT_REASONING_EFFORT"] == "default"
