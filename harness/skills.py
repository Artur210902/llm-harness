"""Skills: external, versioned methodology modules injected into sub-agents at runtime.

A skill is `skills/<name>/SKILL.md`: YAML front matter (metadata the dispatcher sees)
plus a Markdown body (the full text only the target sub-agent receives). Rules inside
the body are tagged `**ABC-01**` so that sub-agents can cite them and the harness can
check the citations.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_RULE_ID = re.compile(r"\*\*([A-Z]{2,5}-\d{2})\*\*")


class SkillError(Exception):
    pass


class Skill(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str = "1.0"
    description: str
    applies_to: tuple[str, ...]
    triggers: tuple[str, ...] = ()
    body: str
    source: str

    @property
    def rule_ids(self) -> frozenset[str]:
        return frozenset(_RULE_ID.findall(self.body))

    @property
    def approx_tokens(self) -> int:
        return max(1, len(self.body) // 4)

    def trigger_in(self, text: str) -> str | None:
        lowered = text.lower()
        return next((t for t in self.triggers if t.lower() in lowered), None)

    def render(self) -> str:
        return f'<skill name="{self.name}" version="{self.version}">\n{self.body.strip()}\n</skill>'


@dataclass(frozen=True)
class SkillAttachment:
    skill: Skill
    source: Literal["planner", "auto-trigger"]
    reason: str


class SkillRegistry:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        paths = sorted(skills_dir.glob("*/SKILL.md"))
        self._skills = {s.name: s for s in (self._load(p) for p in paths)}
        if not self._skills:
            raise SkillError(f"no skills found in {skills_dir}")

    def _load(self, path: Path) -> Skill:
        match = _FRONT_MATTER.match(path.read_text(encoding="utf-8"))
        if not match:
            raise SkillError(f"{path}: missing YAML front matter")
        meta = yaml.safe_load(match.group(1)) or {}
        try:
            skill = Skill(**meta, body=match.group(2), source=path.relative_to(self.skills_dir).as_posix())
        except ValidationError as exc:
            raise SkillError(f"{path}: invalid skill metadata: {exc}") from exc
        if skill.name != path.parent.name:
            raise SkillError(f"{path}: name '{skill.name}' must match its directory")
        return skill

    def __contains__(self, name: object) -> bool:
        return name in self._skills

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def get(self, name: str) -> Skill:
        return self._skills[name]

    def catalog(self) -> str:
        """Metadata only - the dispatcher never sees skill bodies (progressive disclosure)."""
        return "\n".join(
            f"- {s.name} (v{s.version}): {s.description} | applies_to: {', '.join(s.applies_to)}"
            for s in self
        )

    def resolve(
        self, agent: str, requested: Iterable[str], task_text: str
    ) -> tuple[list[SkillAttachment], list[str]]:
        """Skills to inject into `agent`: the planner's choice plus auto-triggered ones.

        Returns the attachments and warnings about requests that were refused.
        """
        attachments: list[SkillAttachment] = []
        warnings: list[str] = []
        for name in dict.fromkeys(requested):
            if name not in self._skills:
                warnings.append(f"unknown skill '{name}' ignored")
            elif agent not in self._skills[name].applies_to:
                warnings.append(f"skill '{name}' does not apply to {agent}; not injected")
            else:
                attachments.append(SkillAttachment(self._skills[name], "planner", "selected by dispatcher"))
        chosen = {a.skill.name for a in attachments}
        for skill in self:
            if skill.name in chosen or agent not in skill.applies_to:
                continue
            if trigger := skill.trigger_in(task_text):
                attachments.append(SkillAttachment(skill, "auto-trigger", f'matched "{trigger}"'))
        return attachments, warnings
