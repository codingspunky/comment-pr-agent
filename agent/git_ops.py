"""Git working-tree operations via the git CLI.

A working tree is required anyway (we run the test suite against the edits),
so commits go through git rather than the Git Data API.

Credentials are passed per-command via `http.extraHeader` and never written to
`.git/config`. The previous approach embedded the installation token in the
remote URL, which left a live credential inside the working tree the model is
about to be shown -- `read_targets` now refuses `.git/`, but not storing the
token there in the first place is the durable fix.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence


class GitError(RuntimeError):
    pass


def _auth_header(token: str) -> str:
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return f"http.extraHeader=AUTHORIZATION: basic {basic}"


def _redact(text: str, token: Optional[str] = None) -> str:
    """Scrub credentials from git output.

    Covers three shapes: the `x-access-token:<token>@` form git echoes when a
    remote URL carries credentials, the base64 blob in an Authorization header
    (which is what we now pass), and the raw token itself.
    """
    out = text or ""
    if token:
        out = out.replace(token, "***")
        try:
            basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
            out = out.replace(basic, "***")
        except Exception:                      # pragma: no cover - defensive
            pass
    lines: List[str] = []
    for line in out.splitlines():
        if "x-access-token:" in line and "@" in line:
            head, _, rest = line.partition("x-access-token:")
            _, _, tail = rest.partition("@")
            line = head + "x-access-token:***@" + tail
        if "AUTHORIZATION: basic " in line:
            head, _, _rest = line.partition("AUTHORIZATION: basic ")
            line = head + "AUTHORIZATION: basic ***"
        lines.append(line)
    return "\n".join(lines)


def _run(args: List[str], cwd: Optional[str] = None, token: Optional[str] = None) -> str:
    env = dict(os.environ)
    # Never let git stop for an interactive credential prompt in a service.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(
            f"`{_redact(' '.join(args), token)}` failed ({proc.returncode}):\n"
            f"{_redact(proc.stderr, token)}")
    return proc.stdout


def _authed(token: str, args: Sequence[str]) -> List[str]:
    """Prefix a git invocation with a one-shot credential header."""
    return ["git", "-c", _auth_header(token), *args]


@dataclass
class Workspace:
    path: str
    branch: str


def clone(repo: str, token: str, dest: str, api_host: str = "github.com") -> str:
    """Clone `repo` into `dest` without persisting the credential.

    Blobs are fetched lazily (`--filter=blob:none`): the agent reads a handful
    of files, so pulling every blob in history is wasted time and disk on any
    sizeable repository. History stays reachable, so any branch can be checked
    out.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    url = f"https://{api_host}/{repo}.git"
    _run(_authed(token, ["clone", "--filter=blob:none", "--no-tags", url, dest]), token=token)
    return dest


def configure_identity(path: str, name: str, email: str) -> None:
    _run(["git", "config", "user.name", name], cwd=path)
    _run(["git", "config", "user.email", email], cwd=path)


def checkout_existing(path: str, branch: str, token: str) -> Workspace:
    """Check out an existing remote branch (the open PR's feature branch)."""
    _run(_authed(token, ["fetch", "origin", branch]), cwd=path, token=token)
    _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=path)
    return Workspace(path=path, branch=branch)


def checkout_new(path: str, base: str, new_branch: str, token: str) -> Workspace:
    """Create a new branch off `base`."""
    _run(_authed(token, ["fetch", "origin", base]), cwd=path, token=token)
    _run(["git", "checkout", "-B", new_branch, f"origin/{base}"], cwd=path)
    return Workspace(path=path, branch=new_branch)


def reset_worktree(path: str) -> None:
    """Discard every uncommitted change, tracked or not.

    Called before each retry so the model's next attempt starts from the same
    pristine tree it is shown in the prompt, and so files created by a
    discarded attempt cannot survive into the commit.
    """
    _run(["git", "reset", "--hard", "HEAD"], cwd=path)
    _run(["git", "clean", "-fdx"], cwd=path)


def has_changes(path: str) -> bool:
    out = _run(["git", "status", "--porcelain"], cwd=path)
    return bool(out.strip())


def commit_paths(path: str, message: str, paths: Sequence[str]) -> Optional[str]:
    """Stage exactly `paths` and commit. Returns the SHA, or None if nothing changed.

    Deliberately not `git add -A`: validation runs before the commit and
    installs dependencies (`pip install -e .` leaves *.egg-info, `npm ci`
    leaves node_modules), so staging the whole tree commits build output
    whenever the target repo's .gitignore does not happen to cover it.
    `git add -A -- <path>` also stages deletions for that path.
    """
    unique = [p for p in dict.fromkeys(paths) if p]
    if not unique:
        return None
    _run(["git", "add", "-A", "--", *unique], cwd=path)
    staged = _run(["git", "diff", "--cached", "--name-only"], cwd=path).strip()
    if not staged:
        return None
    _run(["git", "commit", "-m", message], cwd=path)
    return _run(["git", "rev-parse", "HEAD"], cwd=path).strip()


def diff_paths(path: str, paths: Sequence[str], max_chars: int = 6000) -> str:
    """Unified diff of the working tree against HEAD for `paths`.

    Fed back into the retry prompt so the model can see what its previous
    attempt actually did rather than re-deriving from the original file.
    """
    unique = [p for p in dict.fromkeys(paths) if p]
    if not unique:
        return ""
    try:
        out = _run(["git", "diff", "--", *unique], cwd=path)
    except GitError:
        return ""
    return out[:max_chars]


def push(path: str, branch: str, token: str) -> None:
    _run(_authed(token, ["push", "-u", "origin", branch]), cwd=path, token=token)
