"""Work out how to validate an arbitrary repository.

The agent is pointed at repositories it has never seen. Requiring an operator to
hand-configure `AGENT_VALIDATE_CMD` defeats that: the setting is global to the
agent installation, so one repo's command is applied to every repo, and the
second repo you point it at fails for reasons that have nothing to do with the
edit.

So detection is the layer that has to carry the general case. What was here
before only recognised `pyproject.toml`, `setup.py` and `package.json`, all at
the repository ROOT. Two consequences:

  - `requirements.txt` -- probably the most common way a Python repo declares
    dependencies -- was not detected at all;
  - a repo that keeps its project one directory down matched nothing, which is
    ordinary in repos that hold more than one thing.

This module is pure: it inspects the filesystem and returns a description. It
runs nothing. That keeps the interesting logic testable without executing a
stranger's build.

WHAT IT CANNOT DO. Detection is a heuristic and there is a long tail it will
miss: unusual layouts, integration tests that need a database, monorepos with
several test suites. Those are what the repo-supplied `validate_cmd` and the
global env override exist for. The aim is "most repos with no configuration,
the rest declare it" -- not "every repo, always".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

# Directories never searched for manifests. A vendored dependency's own
# package.json is not this repository's test suite.
SKIP_DIRS = {
    ".git", ".github", "node_modules", "venv", ".venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
    "vendor", ".tox", ".idea", ".vscode", "site-packages", ".eggs",
}

# How deep below the root to look for a manifest. 1 is deliberate: it covers the
# common "project lives in a subdirectory" layout without turning detection into
# a search that might find a fixture's package.json six levels down.
MAX_DEPTH = 1


@dataclass(frozen=True)
class Detection:
    """How to validate a repository.

    `workdir` is relative to the repo root and is where the command runs.
    `setup` installs dependencies; `test` runs the suite. They are kept separate
    so the caller can run `setup` inside an isolated environment -- joining them
    with `&&` into one string would make that impossible.
    """
    ecosystem: str
    test: str
    setup: Optional[str] = None
    workdir: str = ""
    manifest: str = ""

    @property
    def command(self) -> str:
        """The whole thing as one shell command.

        `&&`, never `;`: with `;` a failed install is swallowed and the tests run
        against a stale environment, reporting green for the wrong reason.
        """
        return f"{self.setup} && {self.test}" if self.setup else self.test


def _candidate_dirs(repo_path: str) -> List[str]:
    """Root first, then immediate subdirectories, sorted.

    Sorted because `os.listdir` order is arbitrary: an unsorted search would
    pick a different subdirectory between runs on the same repository.
    """
    dirs = [""]
    try:
        entries = sorted(os.listdir(repo_path))
    except OSError:
        return dirs
    for name in entries:
        if name in SKIP_DIRS or name.startswith("."):
            continue
        if os.path.isdir(os.path.join(repo_path, name)):
            dirs.append(name)
    return dirs[:1 + 64]


def _exists(repo_path: str, workdir: str, name: str) -> bool:
    return os.path.isfile(os.path.join(repo_path, workdir, name))


def _requirements_in(repo_path: str, workdir: str) -> Optional[str]:
    """The most plausible requirements file, or None.

    Prefers a plain `requirements.txt`. A `requirements-dev.txt` usually
    includes the runtime set, so it is a reasonable second choice, but guessing
    beyond that does more harm than good.
    """
    for name in ("requirements.txt", "requirements-dev.txt", "requirements/base.txt"):
        if _exists(repo_path, workdir, name):
            return name
    return None


def _makefile_has_test(repo_path: str, workdir: str) -> bool:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = os.path.join(repo_path, workdir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # A target definition starts at column zero: `test:` or
                    # `test: deps`. Indented `test:` is a recipe line, not a
                    # target, and `.PHONY: test` is a declaration.
                    if line.startswith(("test:", "test :", "check:", "check :")):
                        return True
        except OSError:
            continue
    return False


def _detect_in(repo_path: str, workdir: str) -> Optional[Detection]:
    """First ecosystem match in one directory, or None.

    Order matters. Python and Node come first because they are the common cases;
    Make comes LAST because a `test:` target often just delegates to whichever
    of the others is already present, and calling the tool directly gives better
    failure output than calling make around it.
    """
    # --- Python -----------------------------------------------------------
    has_pyproject = _exists(repo_path, workdir, "pyproject.toml")
    has_setup = _exists(repo_path, workdir, "setup.py")
    requirements = _requirements_in(repo_path, workdir)

    if has_pyproject or has_setup:
        # An editable install pulls the project's own declared dependencies,
        # which is more reliable than a requirements file that may only pin a
        # subset.
        return Detection(ecosystem="python", setup="{pip} install -q -e .",
                         test="{python} -m pytest -q", workdir=workdir,
                         manifest="pyproject.toml" if has_pyproject else "setup.py")
    if requirements:
        return Detection(ecosystem="python",
                         setup="{pip} install -q -r " + requirements,
                         test="{python} -m pytest -q", workdir=workdir,
                         manifest=requirements)

    # --- Node -------------------------------------------------------------
    if _exists(repo_path, workdir, "package.json"):
        # `npm ci` requires a lockfile and fails without one; `npm install` is
        # the fallback rather than a failure the operator has to diagnose.
        has_lock = (_exists(repo_path, workdir, "package-lock.json")
                    or _exists(repo_path, workdir, "npm-shrinkwrap.json"))
        install = "npm ci --silent" if has_lock else "npm install --silent --no-audit --no-fund"
        return Detection(ecosystem="node", setup=install, test="npm test --silent",
                         workdir=workdir, manifest="package.json")

    # --- Go ---------------------------------------------------------------
    if _exists(repo_path, workdir, "go.mod"):
        # `go test` resolves modules itself; no separate install step.
        return Detection(ecosystem="go", test="go test ./...",
                         workdir=workdir, manifest="go.mod")

    # --- Rust -------------------------------------------------------------
    if _exists(repo_path, workdir, "Cargo.toml"):
        return Detection(ecosystem="rust", test="cargo test --quiet",
                         workdir=workdir, manifest="Cargo.toml")

    # --- Make (last resort) -----------------------------------------------
    if _makefile_has_test(repo_path, workdir):
        return Detection(ecosystem="make", test="make test",
                         workdir=workdir, manifest="Makefile")
    return None


def detect(repo_path: str) -> Optional[Detection]:
    """Detect how to validate `repo_path`, or None if nothing is recognised.

    Root wins over a subdirectory: a repo with both a root `pyproject.toml` and
    a `frontend/package.json` is a Python project with a frontend, and running
    the root suite is the better default.
    """
    for workdir in _candidate_dirs(repo_path):
        found = _detect_in(repo_path, workdir)
        if found is not None:
            return found
    return None
