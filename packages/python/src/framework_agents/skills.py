"""Skills: portable capability packs (SKILL.md frontmatter + markdown body).

Parity: packages/typescript/src/skills/index.ts. Local skill routing is
dependency-free TF-IDF cosine over skill name+description; explicit `$name`
mentions ALWAYS win first (word-boundary regex — `$art` must not fire on
`$artillery`, nor `$test` on `$testing`). Unlike TS, the idf weight is
applied to BOTH cosine norms (query norm included).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .tools import Tool

FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)
MENTION_TEMPLATE = r"\${}(?![\w-])"

TokenList = list[str]


@dataclass
class Skill:
    name: str
    description: str
    version: str
    path: str
    body: str
    tools: list[Tool] = field(default_factory=list)


@dataclass
class ScoredSkill:
    skill: Skill
    score: float


def _strip_bom(content: str) -> str:
    return content[1:] if content.startswith("﻿") else content


def _parse_meta(fm: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in fm.split("\n"):
        i = line.find(":")
        if i > 0:
            key = line[:i].strip()
            value = line[i + 1 :].strip().strip('"').strip("'")
            meta[key] = value
    return meta


def parse_skill_file(content: str, path: str = "<inline>") -> tuple[dict[str, str], str]:
    """Parse SKILL.md content -> (meta, body). Pure, testable, no FS.

    Tolerates a leading BOM, CRLF line endings, and quoted values.
    """
    raw = _strip_bom(content)
    match = FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"Invalid SKILL.md (missing frontmatter): {path}")
    fm, body = match.group(1), match.group(2)
    meta = _parse_meta(fm)
    if not meta.get("name", "").strip():
        raise ValueError(f"Invalid SKILL.md (missing name): {path}")
    if not meta.get("description", "").strip():
        raise ValueError(f"Invalid SKILL.md (missing description): {path}")
    meta["version"] = meta.get("version") or "0.1.0"
    if not body.strip():
        raise ValueError(f"Invalid SKILL.md (empty body): {path}")
    return meta, body.strip()


class SkillRegistry:
    """Registry of loaded skills — activation is explicit, never magic."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> "SkillRegistry":
        if skill.name in self._skills:
            raise ValueError(f"Skill already registered: {skill.name}")
        self._skills[skill.name] = skill
        return self

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def as_tools(self) -> list[Tool]:
        """Expose each registered skill's tools (if any) for Agent consumption."""
        return [t for s in self.list() for t in (s.tools or [])]


def _skill_from_content(content: str, path: str) -> Skill:
    meta, body = parse_skill_file(content, path)
    return Skill(
        name=meta["name"],
        description=meta["description"],
        version=meta.get("version", "0.1.0"),
        path=path,
        body=body,
        tools=[],
    )


def discover_skills(dirs: Iterable[str], strict: bool = False) -> list[Skill]:
    """Scan directories for SKILL.md files (one level, plus direct files).

    Invalid skills are skipped (or raise when strict). Never raises for
    missing directories — returns what it finds.
    """
    found: list[Skill] = []
    for directory in dirs:
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(directory, entry)
            if os.path.isdir(full):
                file = os.path.join(full, "SKILL.md")
            elif entry.endswith("SKILL.md"):
                file = full
            else:
                continue
            try:
                with open(file, encoding="utf-8") as fh:
                    found.append(_skill_from_content(fh.read(), file))
            except (OSError, ValueError) as err:
                if strict:
                    raise
                _ = err  # skip invalid skill (default: warn-by-omission, as TS)
    return found


def tokenize(text: str) -> list[str]:
    """TS parity: lowercase, split on non-alphanumerics, drop singletons."""
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 1]


def _mention_pattern(name: str) -> re.Pattern[str]:
    return re.compile(MENTION_TEMPLATE.format(re.escape(name)))


def _idf_of(docs: list[list[str]]) -> dict[str, float]:
    df: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    return {
        term: math.log((len(docs) + 1) / (count + 1)) + 1 for term, count in df.items()
    }


def score_skills(registry: SkillRegistry, input_text: str) -> list[ScoredSkill]:
    """TF-IDF cosine over name+description; `$name` mentions win with inf."""
    skills = registry.list()
    docs = [tokenize(f"{s.name} {s.description}") for s in skills]
    idf = _idf_of(docs)
    qtf: dict[str, int] = {}
    for t in tokenize(input_text):
        qtf[t] = qtf.get(t, 0) + 1
    # idf weights BOTH norms (query side included — a deliberate fix over TS,
    # whose query norm was raw tf).
    qnorm = math.sqrt(sum((c * idf.get(t, 1.0)) ** 2 for t, c in qtf.items())) or 1.0

    scored: list[ScoredSkill] = []
    for skill, doc in zip(skills, docs):
        # Word-boundary mention routing always wins FIRST: `$test` must not
        # fire on `$testing`, nor `$art` on `$artillery`.
        if _mention_pattern(skill.name).search(input_text):
            scored.append(ScoredSkill(skill, math.inf))
            continue
        dtf: dict[str, int] = {}
        for t in doc:
            dtf[t] = dtf.get(t, 0) + 1
        dot = 0.0
        dnorm = 0.0
        for term, count in dtf.items():
            w = count * idf.get(term, 1.0)
            dnorm += w * w
            if term in qtf:
                dot += w * qtf[term] * idf.get(term, 1.0)
        denom = math.sqrt(dnorm) * qnorm
        scored.append(ScoredSkill(skill, dot / denom if denom else 0.0))
    return scored


def match_skills(registry: SkillRegistry, input_text: str, threshold: float = 0.05) -> list[Skill]:
    return [
        s.skill
        for s in sorted(score_skills(registry, input_text), key=lambda x: x.score, reverse=True)
        if s.score >= threshold
    ]


__all__ = [
    "ScoredSkill",
    "Skill",
    "SkillRegistry",
    "discover_skills",
    "match_skills",
    "parse_skill_file",
    "score_skills",
    "tokenize",
]