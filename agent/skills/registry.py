"""Skill discovery and progressive disclosure.

Two tiers:
  builtin  -- agent/skills_builtin/*/SKILL.md         (shipped with the agent)
  repo     -- <target repo>/.agent/skills/*/SKILL.md  (owned by the repo team)

A repo skill with the same name REPLACES the builtin one -- repos can override
agent defaults (e.g. their own `pr-conventions`).

State-serialisation note: LangGraph checkpoints state as JSON, so the registry
object itself never enters graph state. Nodes re-discover (a cheap fs walk)
and store only rendered strings.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from .model import Skill, try_parse

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "skills_builtin"
REPO_SKILLS_SUBDIR = Path(".agent") / "skills"


class SkillRegistry:
    def __init__(self, skills: Dict[str, Skill]):
        self._skills = skills

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    @classmethod
    def discover(cls, repo_path: str | None = None) -> "SkillRegistry":
        skills: Dict[str, Skill] = {}
        for skill in cls._scan(BUILTIN_DIR, source="builtin"):
            skills[skill.name] = skill
        if repo_path:
            root = Path(repo_path).resolve()
            repo_dir = (root / REPO_SKILLS_SUBDIR)
            # Containment: `.agent/skills` may be a symlink. `apply_edits` and
            # `read_targets` both resolve-and-contain; this third filesystem
            # entry point has to as well, or a repo can point it at anything
            # readable on the agent host.
            try:
                resolved = repo_dir.resolve()
            except OSError:
                resolved = None
            if resolved is not None and (root in resolved.parents or resolved == root):
                for skill in cls._scan(resolved, source="repo"):
                    skills[skill.name] = skill        # repo overrides builtin
        return cls(skills)

    @staticmethod
    def _scan(root: Path, source: str) -> List[Skill]:
        if not root.is_dir():
            return []
        found: List[Skill] = []
        seen_dirs: set[Path] = set()
        # Case-insensitive match. `glob("*/SKILL.md")` finds `skill.md` on
        # Windows (case-insensitive filesystem) and silently misses it on the
        # Linux host it deploys to -- a skill that works in the demo then
        # vanishes in production with no error.
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if child in seen_dirs:
                continue
            for entry in sorted(child.iterdir()):
                if entry.is_file() and entry.name.lower() == "skill.md":
                    skill = try_parse(entry, source)
                    if skill:
                        found.append(skill)
                    seen_dirs.add(child)
                    break
        return found

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #

    def all(self) -> List[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(str(name).strip().lower())

    def names(self) -> List[str]:
        return [s.name for s in self.all()]

    # ------------------------------------------------------------------ #
    # Progressive disclosure rendering
    # ------------------------------------------------------------------ #

    def render_index(self) -> str:
        """Level 1: one line per on-demand skill. Always-on skills are omitted
        (their bodies ride in every prompt already -- listing them again would
        invite pointless load_skills calls)."""
        lines = [s.index_line for s in self.all() if not s.always]
        return "\n".join(lines) if lines else "(no loadable skills)"

    def render_always(self) -> str:
        """Level 2: full bodies of always-on skills."""
        parts = [s.render() for s in self.all() if s.always]
        return "\n\n".join(parts)

    def load(self, names: List[str]) -> str:
        """Level 3: full bodies of requested skills. Unknown names are reported
        back rather than silently dropped, so the model learns the miss."""
        parts: List[str] = []
        for raw in names:
            skill = self.get(str(raw))
            if skill:
                parts.append(skill.render())
            else:
                parts.append(f"<skill name=\"{raw}\">not found — available: "
                             f"{', '.join(n for n in self.names())}</skill>")
        return "\n\n".join(parts) if parts else "(no skills requested)"

    # ------------------------------------------------------------------ #
    # Skill-sourced configuration
    # ------------------------------------------------------------------ #

    def validate_cmd(self) -> Optional[str]:
        """First repo skill declaring a STRING metadata.validate_cmd wins.

        Builtin skills deliberately cannot set this. A non-string (a YAML list
        or mapping) is rejected rather than coerced: `str(["rm", "-rf"])` used
        to produce a nonsense command that was then handed to `shell=True`.

        The caller decides whether to honour the result at all -- see
        AGENT_ALLOW_REPO_VALIDATE_CMD in config.py.
        """
        for skill in self.all():
            if skill.source != "repo":
                continue
            value = skill.metadata.get("validate_cmd")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
