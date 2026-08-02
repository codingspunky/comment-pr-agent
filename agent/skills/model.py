"""The skill contract: a folder with a SKILL.md (YAML frontmatter + markdown body).

Frontmatter schema:
  name: str            (required, unique; repo skills override builtin by name)
  description: str     (required; the ONLY thing the model sees pre-load)
  always: bool         (optional; inject full body into every prompt)
  metadata: dict       (optional; e.g. validate_cmd for repo skills)

Bodies are size-capped at parse time so no single skill can blow the token
budget of a plan/edit call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

MAX_BODY_CHARS = 8_000
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SkillParseError(ValueError):
    """Raised when a SKILL.md is malformed. Carries the offending path."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    source: str                      # "builtin" | "repo"
    path: str                        # directory containing SKILL.md
    always: bool = False
    metadata: dict = field(default_factory=dict)

    @property
    def index_line(self) -> str:
        """One line for the progressive-disclosure index."""
        return f"- {self.name}: {self.description}"

    def render(self) -> str:
        """Full body, framed so the model can tell skills apart."""
        return f"<skill name=\"{self.name}\">\n{self.body}\n</skill>"


def parse_skill_md(path: Path, source: str) -> Skill:
    """Parse one SKILL.md into a Skill. Raises SkillParseError on bad input."""
    raw = path.read_text(encoding="utf-8", errors="replace")

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise SkillParseError(f"{path}: missing YAML frontmatter (--- ... ---)")

    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillParseError(f"{path}: frontmatter must be a YAML mapping")

    name = str(meta.get("name", "")).strip().lower()
    description = str(meta.get("description", "")).strip()
    if not _NAME_RE.match(name):
        raise SkillParseError(
            f"{path}: 'name' is required and must match {_NAME_RE.pattern}")
    if not description:
        raise SkillParseError(f"{path}: 'description' is required")

    body = raw[m.end():].strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n[skill body truncated]"

    extra = meta.get("metadata") or {}
    if not isinstance(extra, dict):
        extra = {}

    return Skill(
        name=name,
        description=description,
        body=body,
        source=source,
        path=str(path.parent),
        always=bool(meta.get("always", False)),
        metadata=extra,
    )


def try_parse(path: Path, source: str) -> Optional[Skill]:
    """Parse, returning None on malformed skills (a bad repo skill must never
    take the whole run down — it just doesn't load)."""
    try:
        return parse_skill_md(path, source)
    except (SkillParseError, OSError):
        return None
