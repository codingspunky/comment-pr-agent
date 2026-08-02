"""Tests for `agent/detect.py` and the isolated validation environment.

The problem being fixed: `AGENT_VALIDATE_CMD` is global to the agent
installation, so hand-configuring it for one repository breaks every other one.
Detection is the layer that has to work on a repo nobody configured.
"""
from __future__ import annotations

import os

from agent import detect as detect_mod
from agent.config import Settings
from agent.sandbox import _detect_command, cleanup_validation_env


def _touch(root, relative, content=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The two gaps that sent an operator to hand-configuration
# --------------------------------------------------------------------------- #

def test_requirements_txt_is_detected(tmp_path):
    """Probably the most common way a Python repo declares dependencies, and the
    old detection did not look for it at all -- it checked only pyproject.toml,
    setup.py and package.json."""
    _touch(tmp_path, "requirements.txt", "flask\n")
    found = detect_mod.detect(str(tmp_path))
    assert found is not None
    assert found.ecosystem == "python"
    assert found.manifest == "requirements.txt"
    assert "-r requirements.txt" in found.setup


def test_a_project_one_level_down_is_detected(tmp_path):
    """Detection only looked at the repository ROOT, so a repo holding its
    project in a subdirectory matched nothing and fell through to 'skipped'."""
    _touch(tmp_path, "aiops/requirements.txt", "sqlalchemy\n")
    _touch(tmp_path, "aiops/smoke_test.py", "def test_x(): pass\n")
    found = detect_mod.detect(str(tmp_path))
    assert found is not None
    assert found.ecosystem == "python"
    assert found.workdir == "aiops"


def test_root_wins_over_a_subdirectory(tmp_path):
    """A Python project with a frontend/ is a Python project."""
    _touch(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _touch(tmp_path, "frontend/package.json", "{}")
    found = detect_mod.detect(str(tmp_path))
    assert found.ecosystem == "python"
    assert found.workdir == ""


# --------------------------------------------------------------------------- #
# Ecosystem coverage
# --------------------------------------------------------------------------- #

def test_pyproject_prefers_editable_install_over_requirements(tmp_path):
    """A project's own metadata is more reliable than a requirements file that
    may pin only a subset."""
    _touch(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _touch(tmp_path, "requirements.txt", "flask\n")
    found = detect_mod.detect(str(tmp_path))
    assert "-e ." in found.setup


def test_npm_ci_only_with_a_lockfile(tmp_path):
    """`npm ci` fails outright without a lockfile. Falling back to `npm install`
    beats handing the operator an error to diagnose."""
    _touch(tmp_path, "package.json", "{}")
    assert "npm install" in detect_mod.detect(str(tmp_path)).setup
    _touch(tmp_path, "package-lock.json", "{}")
    assert detect_mod.detect(str(tmp_path)).setup == "npm ci --silent"


def test_go_and_rust_need_no_install_step(tmp_path):
    """Both toolchains resolve dependencies as part of the test command."""
    _touch(tmp_path, "go.mod", "module x\n")
    go = detect_mod.detect(str(tmp_path))
    assert go.ecosystem == "go" and go.setup is None
    assert go.command == "go test ./..."

    other = tmp_path / "rust"
    _touch(other, "Cargo.toml", "[package]\nname='x'\n")
    rust = detect_mod.detect(str(other))
    assert rust.ecosystem == "rust" and rust.setup is None


def test_makefile_is_a_last_resort(tmp_path):
    """A `test:` target usually delegates to whichever real tool is present, and
    calling that tool directly gives better failure output."""
    _touch(tmp_path, "Makefile", "test:\n\tpytest\n")
    _touch(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert detect_mod.detect(str(tmp_path)).ecosystem == "python"

    only_make = tmp_path / "mk"
    _touch(only_make, "Makefile", "build:\n\tcc x.c\ntest:\n\t./a.out\n")
    assert detect_mod.detect(str(only_make)).ecosystem == "make"


def test_a_phony_declaration_is_not_a_test_target(tmp_path):
    """`.PHONY: test` declares a target elsewhere; an indented `test:` is a
    recipe line. Neither means `make test` will work."""
    _touch(tmp_path, "Makefile", ".PHONY: test\nbuild:\n\techo test:\n")
    assert detect_mod.detect(str(tmp_path)) is None


def test_unknown_repo_detects_nothing(tmp_path):
    """Returning None is correct -- the caller then skips validation and says so
    in the PR body, rather than inventing a command that will fail."""
    _touch(tmp_path, "README.md", "# hi\n")
    assert detect_mod.detect(str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# Search hygiene
# --------------------------------------------------------------------------- #

def test_vendored_manifests_are_ignored(tmp_path):
    """A dependency's own package.json is not this repository's test suite."""
    _touch(tmp_path, "node_modules/left-pad/package.json", "{}")
    _touch(tmp_path, "venv/lib/pyproject.toml", "[project]\nname='dep'\n")
    _touch(tmp_path, ".git/config", "")
    assert detect_mod.detect(str(tmp_path)) is None


def test_subdirectory_choice_is_deterministic(tmp_path):
    """os.listdir order is arbitrary; an unsorted search would pick a different
    subdirectory between runs on the same repository."""
    for name in ("zeta", "alpha", "middle"):
        _touch(tmp_path, f"{name}/requirements.txt", "x\n")
    picks = {detect_mod.detect(str(tmp_path)).workdir for _ in range(5)}
    assert picks == {"alpha"}


def test_setup_and_test_stay_separate(tmp_path):
    """Joined into one string, the caller could not run the install inside an
    isolated environment -- which is the whole point of keeping them apart."""
    _touch(tmp_path, "requirements.txt", "x\n")
    found = detect_mod.detect(str(tmp_path))
    assert found.setup and found.test
    assert " && " in found.command


# --------------------------------------------------------------------------- #
# Wiring into sandbox
# --------------------------------------------------------------------------- #

def test_detected_python_command_installs_pytest(tmp_path):
    """pytest is not guaranteed to be a dependency of the project under test,
    and in a fresh virtualenv it certainly is not there yet."""
    _touch(tmp_path, "requirements.txt", "x\n")
    cmd = _detect_command(str(tmp_path), Settings(isolated_validate=False))
    assert "install -q pytest" in cmd
    assert "-r requirements.txt" in cmd


def test_agent_yml_still_outranks_detection(tmp_path):
    """Repo-supplied shell stays behind its opt-in gate, and still wins when on."""
    _touch(tmp_path, ".agent.yml", "validate: echo custom\n")
    _touch(tmp_path, "requirements.txt", "x\n")
    off = _detect_command(str(tmp_path), Settings(allow_repo_validate_cmd=False,
                                                 isolated_validate=False))
    on = _detect_command(str(tmp_path), Settings(allow_repo_validate_cmd=True,
                                                isolated_validate=False))
    assert "requirements.txt" in off        # gate closed -> detection
    assert on == "echo custom"              # gate open -> repo wins


def test_isolated_validation_uses_a_venv_outside_the_worktree(tmp_path):
    """`reset_worktree` runs `git clean -fdx` between retries, which deletes
    ignored files -- a venv inside the repo would vanish mid-run."""
    repo = tmp_path / "repo"
    _touch(repo, "requirements.txt", "x\n")
    try:
        cmd = _detect_command(str(repo), Settings(isolated_validate=True))
        assert cmd is not None
        # The interpreter path in the command must not sit under the repo.
        assert str(repo) not in cmd
        assert "-m pip install" in cmd
    finally:
        cleanup_validation_env(str(repo))


def test_venv_is_reused_across_retries(tmp_path):
    """A retry must not pay to rebuild the environment."""
    _touch(tmp_path, "requirements.txt", "x\n")
    try:
        first = _detect_command(str(tmp_path), Settings(isolated_validate=True))
        second = _detect_command(str(tmp_path), Settings(isolated_validate=True))
        assert first == second
    finally:
        cleanup_validation_env(str(tmp_path))


def test_cleanup_removes_the_venv_and_is_idempotent(tmp_path):
    _touch(tmp_path, "requirements.txt", "x\n")
    cmd = _detect_command(str(tmp_path), Settings(isolated_validate=True))
    venv_python = cmd.split('"')[1]
    assert os.path.isfile(venv_python)
    cleanup_validation_env(str(tmp_path))
    assert not os.path.isfile(venv_python)
    cleanup_validation_env(str(tmp_path))       # must not raise
