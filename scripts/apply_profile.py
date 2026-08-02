"""Merge an environment profile into an existing .env, in place.

The problem this solves: `cp .env.free-tier .env` is destructive. It overwrites
the credentials you already filled in, and the failure is silent -- the next run
just reports missing settings, or worse, picks up a placeholder.

Rules, in order of importance:

  1. A key the profile leaves EMPTY never overwrites a CREDENTIAL you already
     have. That is how secrets survive: the profiles ship `GROQ_API_KEY=` with no
     value, so your key is preserved rather than blanked.

     For ordinary settings an empty value is an instruction, not an omission: it
     CLEARS the key. Needed because clearing is sometimes the whole point --
     `AGENT_VALIDATE_CMD` must go back to empty for per-repo detection to apply,
     and a rule that refused to clear it would leave every existing install
     pinned to one repository's command forever.
  2. A key the profile gives a value to WINS. That is the entire point of applying
     a profile -- AGENT_MAX_TOKENS is meant to change.
  3. A key in your .env that the profile does not mention is left exactly alone.
  4. Keys the profile adds that you do not have yet are appended, so an .env
     written before a newer version still picks up new settings.
  5. Your comments, ordering and spacing survive; only values on matched lines
     move.

A timestamped backup is written before anything changes.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

# Keys whose existing value is kept even when the profile supplies one. These are
# machine- or deployment-specific: a profile has no business asserting where your
# private key lives or what your webhook secret is.
NEVER_OVERWRITE = {
    "GROQ_API_KEY",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_B64",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_WEBHOOK_SECRET",
    "DATABASE_URL",
}


def parse_env(text: str) -> Dict[str, str]:
    """Key/value pairs from a .env body. Comments and blanks are ignored."""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def merge(current_text: str, profile_text: str) -> Tuple[str, List[str], List[str]]:
    """Return (new_text, changed_keys, added_keys).

    Rewrites values on the lines that already exist rather than regenerating the
    file, so comments and ordering are preserved.
    """
    current = parse_env(current_text)
    profile = parse_env(profile_text)

    # Which profile keys should actually be applied?
    applying: Dict[str, str] = {}
    for key, value in profile.items():
        if key in NEVER_OVERWRITE and current.get(key):
            continue                      # rule 1: credentials are yours
        if not value and key not in current:
            continue                      # nothing to clear; do not add a blank
        if current.get(key) == value:
            continue                      # already correct; not a change
        applying[key] = value

    changed: List[str] = []
    out: List[str] = []
    for line in current_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in applying:
                out.append(f"{key}={applying[key]}")
                changed.append(key)
                continue
        out.append(line)

    added = [k for k in applying if k not in current]
    if added:
        out.append("")
        out.append(f"# ---- added by apply-profile ({time.strftime('%Y-%m-%d')}) ----")
        for key in added:
            out.append(f"{key}={applying[key]}")

    return "\n".join(out) + "\n", changed, added


def main(argv: List[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: apply_profile <profile-name>   e.g. free-tier", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parent.parent
    profile_path = root / f".env.{argv[0]}"
    env_path = root / ".env"

    if not profile_path.is_file():
        # Backups are `.env.backup.<timestamp>`, so an endswith() filter misses
        # them and offers the user their own backups as profiles.
        available = sorted(p.name for p in root.glob(".env.*")
                           if ".backup" not in p.name and not p.name.endswith(".bak"))
        print(f"no such profile: {profile_path.name}", file=sys.stderr)
        print(f"available: {', '.join(available) or '(none)'}", file=sys.stderr)
        return 2

    profile_text = profile_path.read_text(encoding="utf-8")

    if not env_path.is_file():
        # Nothing to preserve -- a straight copy is correct here, and saying so
        # is better than pretending a merge happened.
        env_path.write_text(profile_text, encoding="utf-8")
        print(f"created .env from {profile_path.name} (no existing .env to merge)")
        print("Now fill in: GROQ_API_KEY, GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH")
        return 0

    current_text = env_path.read_text(encoding="utf-8")
    new_text, changed, added = merge(current_text, profile_text)

    if not changed and not added:
        print(f".env already matches {profile_path.name}; nothing to do")
        return 0

    backup = env_path.with_suffix(f".backup.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(env_path, backup)
    env_path.write_text(new_text, encoding="utf-8")

    print(f"applied {profile_path.name} to .env  (backup: {backup.name})")
    if changed:
        print(f"  updated: {', '.join(sorted(changed))}")
    if added:
        print(f"  added:   {', '.join(sorted(added))}")

    kept = sorted(k for k in NEVER_OVERWRITE if parse_env(current_text).get(k))
    if kept:
        print(f"  kept your existing: {', '.join(kept)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
