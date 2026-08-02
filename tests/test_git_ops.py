"""git_ops against a real repository on disk."""
from __future__ import annotations

import subprocess

import pytest

from agent import git_ops


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "keep.py").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _tracked(repo):
    out = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_commit_paths_ignores_untracked_build_output(tmp_path):
    """The scenario `git add -A` got wrong: validation runs BEFORE the commit
    and leaves *.egg-info / node_modules behind, which then got committed
    whenever the target repo's .gitignore did not happen to cover them."""
    repo = _init_repo(tmp_path)
    (repo / "keep.py").write_text("edited\n", encoding="utf-8")
    (repo / "x.egg-info").mkdir()
    (repo / "x.egg-info" / "PKG-INFO").write_text("junk", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "left.js").write_text("junk", encoding="utf-8")

    sha = git_ops.commit_paths(str(repo), "edit keep.py", ["keep.py"])
    assert sha
    tracked = _tracked(repo)
    assert "keep.py" in tracked
    assert not any(t.startswith(("x.egg-info", "node_modules")) for t in tracked)


def test_commit_paths_stages_deletions(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "keep.py").unlink()
    sha = git_ops.commit_paths(str(repo), "remove keep.py", ["keep.py"])
    assert sha
    assert "keep.py" not in _tracked(repo)


def test_commit_paths_returns_none_when_nothing_changed(tmp_path):
    repo = _init_repo(tmp_path)
    assert git_ops.commit_paths(str(repo), "noop", ["keep.py"]) is None
    assert git_ops.commit_paths(str(repo), "noop", []) is None


def test_reset_worktree_discards_tracked_and_untracked(tmp_path):
    """A retry must start from the pristine tree, or files created by a
    discarded attempt survive into the commit."""
    repo = _init_repo(tmp_path)
    (repo / "keep.py").write_text("broken\n", encoding="utf-8")
    (repo / "stray.py").write_text("from attempt 1\n", encoding="utf-8")

    git_ops.reset_worktree(str(repo))

    assert (repo / "keep.py").read_text() == "original\n"
    assert not (repo / "stray.py").exists()
    assert not git_ops.has_changes(str(repo))


def test_diff_paths_shows_the_attempt(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "keep.py").write_text("changed\n", encoding="utf-8")
    diff = git_ops.diff_paths(str(repo), ["keep.py"])
    assert "+changed" in diff and "-original" in diff


def test_auth_header_never_lands_in_git_config(tmp_path):
    """The token used to be embedded in the remote URL, which persisted it in
    .git/config inside the tree the model is about to be shown."""
    repo = _init_repo(tmp_path)
    args = git_ops._authed("ghs_secrettoken", ["fetch", "origin", "main"])
    assert args[0] == "git" and args[1] == "-c"
    assert "extraHeader" in args[2]
    config = (repo / ".git" / "config").read_text()
    assert "ghs_secrettoken" not in config
    assert "extraheader" not in config.lower()


def test_git_error_is_redacted(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(git_ops.GitError) as exc:
        git_ops._run(git_ops._authed("ghs_secrettoken", ["fetch", "nonexistent-remote"]),
                     cwd=str(repo), token="ghs_secrettoken")
    assert "ghs_secrettoken" not in str(exc.value)
