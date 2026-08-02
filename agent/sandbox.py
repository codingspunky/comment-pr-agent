"""Validation: run the repo's checks against the agent's edits.

The command is resolved in this order:
  1. a repo skill's `metadata.validate_cmd` -- ONLY when
     AGENT_ALLOW_REPO_VALIDATE_CMD=true (see below)
  2. AGENT_VALIDATE_CMD env / settings.validate_cmd (explicit, recommended)
  3. a `.agent.yml` file in the repo root with a `validate:` key -- same
     opt-in gate as (1): it is also repo-supplied
  4. auto-detection from project files -- see `agent/detect.py`. This is the
     layer that has to work on a repo nobody configured, because (2) is global
     to the agent installation: one repo's command applied to every repo.
  5. if nothing is found: skip (configurable) so the agent still produces a PR

ISOLATION: a detected Python command installs the target repo's dependencies.
Installing them into the agent's own environment lets a target repo's pins
collide with `groq` or `langgraph` and break the agent itself -- and pip runs
the repo's build backend, which is arbitrary code. So detected Python setup runs
in a throwaway virtualenv created OUTSIDE the worktree (`git clean -fdx` between
retries would delete one inside it) and destroyed when the run ends. The venv is
cached per repo path so a retry does not pay to build it twice.

SECURITY: this runs project code. Two distinct risks, often conflated:

  - Running the repo's OWN tests is inherent to the job. Isolate the service
    (container, no network, no host mounts) and that risk is bounded.
  - Running a SHELL COMMAND the repo supplies (a skill's validate_cmd, or
    .agent.yml) is different: it turns "can commit to a branch" into "can run
    code on the machine holding the GitHub App private key", which mints
    tokens for every installation. That is why it is now opt-in and off by
    default. Turn it on when the agent runs on an ephemeral runner (GitHub
    Actions), where the credential is already scoped to one repo and one job.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, Optional

from . import detect as detect_mod
from .commands import redact_secrets
from .config import Settings, get_settings

log = logging.getLogger("comment-pr-agent.sandbox")

# repo_path -> virtualenv directory, so the retry attempt reuses the first
# attempt's environment instead of rebuilding it.
_VENV_CACHE: Dict[str, str] = {}


@dataclass
class ValidationResult:
    ran: bool
    ok: bool
    command: str
    output: str


def _agent_yml_command(repo_path: str, settings: Settings) -> Optional[str]:
    """A `validate:` key from the repo's .agent.yml -- repo-supplied, so gated."""
    if not settings.allow_repo_validate_cmd:
        return None
    cfg = os.path.join(repo_path, ".agent.yml")
    if not os.path.exists(cfg):
        return None
    try:
        import yaml  # optional dependency
        with open(cfg, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        value = data.get("validate")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass
    return None


def _venv_python(venv_dir: str) -> str:
    """Interpreter path inside a virtualenv, on either platform."""
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _ensure_venv(repo_path: str, settings: Settings) -> Optional[str]:
    """Create (or reuse) a throwaway virtualenv for validating this repo.

    Outside the worktree on purpose: `reset_worktree` runs `git clean -fdx`
    between retries, which deletes ignored files -- a venv placed in the repo
    would be destroyed halfway through the run.

    Returns None if creation fails, so the caller can fall back to running in
    the ambient environment rather than failing the run over an optional
    hardening measure.
    """
    cached = _VENV_CACHE.get(repo_path)
    if cached and os.path.isfile(_venv_python(cached)):
        return cached

    venv_dir = tempfile.mkdtemp(prefix="agent-validate-")
    try:
        proc = subprocess.run(
            # NOT --system-site-packages. Seeing the agent's own packages would
            # let a repo pass validation without declaring a dependency it
            # actually needs -- a false green, which is the precise failure the
            # validation footer exists to prevent. Slower, and correct.
            [sys.executable, "-m", "venv", venv_dir],
            capture_output=True, text=True, timeout=settings.validate_setup_timeout,
        )
        if proc.returncode != 0 or not os.path.isfile(_venv_python(venv_dir)):
            log.warning("could not create validation venv; using ambient environment")
            shutil.rmtree(venv_dir, ignore_errors=True)
            return None
    except (OSError, subprocess.SubprocessError):
        log.warning("could not create validation venv; using ambient environment",
                    exc_info=True)
        shutil.rmtree(venv_dir, ignore_errors=True)
        return None

    _VENV_CACHE[repo_path] = venv_dir
    return venv_dir


# A quoted or bare absolute path whose final component is a Python interpreter.
# Matches the isolated-venv interpreter this module injects on both platforms:
#   "C:\Users\...\Temp\agent-validate-xxxx\Scripts\python.exe"
#   /tmp/agent-validate-xxxx/bin/python
#
# The lookbehinds are load-bearing. Without them the bare-path branch matches
# inside a URL -- `https://example.com/python-tools.whl` has an `s:/` that looks
# like a drive letter and a `/python` that looks like an interpreter, and the
# result was the mangled `httppython-tools.whl`. A drive letter must not follow
# another letter, and a leading `/` must not follow `:`, a word character, or
# another `/` -- the second slash of `//` was still matching.
_ABS_PYTHON_RE = re.compile(
    r'"[^"]*[\\/](?:python(?:3(?:\.\d+)?)?(?:\.exe)?)"'                # quoted
    r"|(?:(?<![A-Za-z])[A-Za-z]:[\\/]|(?<![:\w/])/)"                    # drive or root
    r"[^\s\"']*[\\/](?:python(?:3(?:\.\d+)?)?(?:\.exe)?)\b",           # ...ending in python
)


def short_command(command: str) -> str:
    """The validate command with absolute interpreter paths reduced to `python`.

    DISPLAY ONLY. The executed command keeps its absolute paths -- the whole
    point of the isolated venv is that it is NOT whatever `python` happens to
    mean on PATH, so rewriting what actually runs would silently undo the
    isolation.

    This exists because the PR footer is read by other people. A footer reading
    `"C:\\Users\\someone\\AppData\\Local\\Temp\\agent-validate-pes8lrl3\\Scripts
    \\python.exe" -m pip install ...` -- three times over -- leaks an operator's
    local paths into a public repository and tells the reviewer nothing they
    can act on.
    """
    if not command:
        return command
    return _ABS_PYTHON_RE.sub("python", command)


def cleanup_validation_env(repo_path: str) -> None:
    """Destroy this repo's validation virtualenv. Safe to call when none exists."""
    venv_dir = _VENV_CACHE.pop(repo_path, None)
    if venv_dir:
        shutil.rmtree(venv_dir, ignore_errors=True)


def _resolve_detected(repo_path: str, settings: Settings) -> Optional[str]:
    """Turn a Detection into a concrete shell command for this machine."""
    found = detect_mod.detect(repo_path)
    if found is None:
        return None

    python, pip = "python", "python -m pip"
    if found.ecosystem == "python" and settings.isolated_validate:
        venv_dir = _ensure_venv(repo_path, settings)
        if venv_dir:
            vpy = _venv_python(venv_dir)
            # Quoted: temp paths on Windows sit under a user profile directory
            # and routinely contain spaces.
            python = f'"{vpy}"'
            pip = f'"{vpy}" -m pip'

    setup = found.setup.format(python=python, pip=pip) if found.setup else None
    test = found.test.format(python=python, pip=pip)
    if found.ecosystem == "python":
        # pytest is not guaranteed to be a dependency of the project being
        # tested, and in an isolated venv it certainly is not present yet.
        setup = f"{setup} && {pip} install -q pytest" if setup else f"{pip} install -q pytest"

    command = f"{setup} && {test}" if setup else test
    log.info("detected %s project (%s%s); validating with: %s",
             found.ecosystem, found.manifest,
             f" in {found.workdir}/" if found.workdir else "", command)
    return command


def _detect_command(repo_path: str, settings: Settings) -> Optional[str]:
    """Backwards-compatible entry point: .agent.yml first, then detection."""
    return _agent_yml_command(repo_path, settings) or _resolve_detected(repo_path, settings)


def _detected_workdir(repo_path: str) -> str:
    """Absolute directory the detected command should run in."""
    found = detect_mod.detect(repo_path)
    if found is None or not found.workdir:
        return repo_path
    return os.path.join(repo_path, found.workdir)


def validate(repo_path: str, settings: Settings | None = None,
             command_override: Optional[str] = None) -> ValidationResult:
    """`command_override` (from a repo skill) wins, but the caller only passes
    one when AGENT_ALLOW_REPO_VALIDATE_CMD is set. Then env, then detection."""
    settings = settings or get_settings()
    detected: Optional[str] = None
    command: Optional[str] = command_override or settings.validate_cmd
    if not command:
        detected = _detect_command(repo_path, settings)
        command = detected

    if not command:
        if settings.skip_validation_if_unknown:
            return ValidationResult(ran=False, ok=True, command="",
                                    output="no validation command detected; skipped")
        return ValidationResult(ran=False, ok=False, command="",
                                output="no validation command detected and skipping is disabled")

    secrets = settings.secret_values()
    # The repo's test suite is arbitrary code and may echo its environment;
    # its output is posted to a PR comment, so strip our secrets from it.
    env = {k: v for k, v in os.environ.items()
           if k not in {"GROQ_API_KEY", "GITHUB_WEBHOOK_SECRET",
                        "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_B64"}}

    # A detected command runs where its manifest lives: `npm test` must run in
    # the directory holding package.json, not the repo root. An explicit or
    # repo-supplied command always runs at the root, because that is the
    # contract those commands were written against.
    cwd = repo_path
    if command is detected:
        cwd = _detected_workdir(repo_path)

    try:
        proc = subprocess.run(
            command, cwd=cwd, shell=True, capture_output=True, text=True,
            timeout=settings.validate_timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # A hung test suite must surface as a validation failure the reviewer
        # sees, not an unhandled exception that kills the run silently.
        raw = exc.stdout or ""
        partial = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        return ValidationResult(
            ran=True, ok=False, command=command,
            output=redact_secrets(
                f"validation timed out after {settings.validate_timeout}s\n" + partial[-3000:],
                secrets))
    combined = (proc.stdout or "") + (proc.stderr or "")
    tail = redact_secrets(combined[-6000:], secrets)   # failures report at the end
    return ValidationResult(ran=True, ok=proc.returncode == 0, command=command, output=tail)
