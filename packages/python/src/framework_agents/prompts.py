"""Prompts: versioned, renderable templates. No magic strings in agent code.

Parity: packages/typescript/src/prompts/index.ts. Sources: inline
PromptTemplate, markdown files with frontmatter (BOM/CRLF tolerant), A/B
variants (weighted random — unbiased via uniform roll over the weight total).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)
VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass
class PromptTemplate:
    name: str
    template: str
    version: str = "0.1.0"

    def render(self, vars: Optional[dict[str, Any]] = None) -> str:
        values = vars or {}

        def sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                raise ValueError(f"Missing prompt var: {key} (prompt: {self.name})")
            return str(values[key])

        return VAR_RE.sub(sub, self.template)


def define_prompt(name: str, template: str, version: str = "0.1.0") -> PromptTemplate:
    """TS parity helper: template + render raising on a missing {{var}}."""
    return PromptTemplate(name=name, template=template, version=version)


def _strip_bom(content: str) -> str:
    return content[1:] if content.startswith("﻿") else content


def _parse_meta(fm: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in fm.split("\n"):
        i = line.find(":")
        if i > 0:
            key = line[:i].strip()
            value = line[i + 1 :].strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            meta[key] = value
    return meta


def load_prompt_file(path: str | Path) -> PromptTemplate:
    """Load `--- name, version ---` frontmatter + markdown body from disk."""
    raw = _strip_bom(Path(path).read_text(encoding="utf-8"))
    match = FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"Invalid prompt file (missing frontmatter): {path}")
    meta = _parse_meta(match.group(1))
    if not meta.get("name"):
        raise ValueError(f"Invalid prompt file (missing name): {path}")
    return define_prompt(meta["name"], match.group(2).strip(), meta.get("version") or "0.1.0")


def _variant_fields(variant: Any) -> tuple[str, str, Optional[str], Optional[float]]:
    if isinstance(variant, dict):
        return (
            variant.get("name", ""),
            variant.get("template", ""),
            variant.get("version"),
            variant.get("weight"),
        )
    return (
        getattr(variant, "name", ""),
        getattr(variant, "template", ""),
        getattr(variant, "version", None),
        getattr(variant, "weight", None),
    )


def ab_prompts(
    variants: list[Any],
    pick: Optional[str] = None,
    weights: Optional[list[float]] = None,
) -> PromptTemplate:
    """A/B variants: pick by weighted traffic split, or an explicit name.

    `pick` matches "name" or "name@version". `weights` (parallel to variants)
    overrides per-variant weights; default weight is 1. The roll is a single
    uniform draw over the weight total — the interval walk is unbiased.
    """
    if not variants:
        raise ValueError("ab_prompts needs at least one variant")
    if pick:
        for variant in variants:
            name, template, version, _ = _variant_fields(variant)
            label = f"{name}@{version}" if version else name
            if label == pick or name == pick:
                return define_prompt(name, template, version or "0.1.0")
        raise ValueError(f"Unknown prompt variant: {pick}")

    effective = [
        weights[i] if weights is not None and i < len(weights) else (_variant_fields(v)[3] or 1)
        for i, v in enumerate(variants)
    ]
    total = sum(effective)
    roll = random.uniform(0, total)
    for variant, weight in zip(variants, effective):
        roll -= weight
        if roll <= 0:
            name, template, version, _ = _variant_fields(variant)
            return define_prompt(name, template, version or "0.1.0")
    # Floating-point misses are astronomically rare; deterministic tail.
    name, template, version, _ = _variant_fields(variants[-1])
    return define_prompt(name, template, version or "0.1.0")


__all__ = [
    "PromptTemplate",
    "ab_prompts",
    "define_prompt",
    "load_prompt_file",
]