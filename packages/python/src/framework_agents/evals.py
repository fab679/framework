"""Evals: JSONL datasets + scoring + pass-rate reporter.

Parity: packages/typescript/src/evals/index.ts. Offline by default (scripted
models). `no_structured_error` is declared for shape parity — a structured
failure surfaces as a thrown error (scored "threw") exactly like TS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent import Agent
from .types import TokenUsage


@dataclass
class EvalExpect:
    contains: list[str] = field(default_factory=list)
    tool_used: list[str] = field(default_factory=list)
    no_structured_error: bool | None = None


@dataclass
class EvalCase:
    id: str
    input: str
    expect: EvalExpect | None = None


# TS names the type EvalCase; the lowercase spelling is a documented alias.
eval_case = EvalCase


@dataclass
class EvalScore:
    id: str
    passed: bool
    detail: str
    usage: TokenUsage


@dataclass
class EvalRun:
    scores: list[EvalScore]
    pass_rate: float


def _expect_from(raw: Any) -> EvalExpect:
    if isinstance(raw, EvalExpect):
        return raw
    if isinstance(raw, dict):
        return EvalExpect(**raw)
    if raw is None:
        return EvalExpect()
    raise ValueError(f"Invalid eval expect: {raw!r}")


def load_jsonl(path: str | Path) -> list[EvalCase]:
    """Read one JSON EvalCase per non-blank line. A UTF-8 BOM (PowerShell
    writes JSONL with one) is stripped before parsing — without it line 1
    would fail json.loads."""
    cases: list[EvalCase] = []
    # utf-8-sig transparently drops a leading BOM when present.
    for line in Path(path).read_text(encoding="utf-8-sig").split("\n"):
        line = line.strip().strip("﻿")
        if not line:
            continue
        raw = json.loads(line)
        cases.append(
            EvalCase(
                id=raw["id"],
                input=raw["input"],
                expect=_expect_from(raw.get("expect")),
            )
        )
    return cases


async def run_evals(
    cases: list[EvalCase],
    make_agent: Callable[[EvalCase], Agent],
) -> EvalRun:
    """Run every case sequentially, scoring text contains + tool usage."""
    scores: list[EvalScore] = []
    for case in cases:
        try:
            res = await make_agent(case).run(case.input)
            expect = case.expect or EvalExpect()
            missing = [s for s in expect.contains if s not in (res.text or "")]
            missing_tools = [
                t for t in expect.tool_used if not any(tc.name == t for tc in res.tool_calls)
            ]
            passed = not missing and not missing_tools
            detail = (
                "ok"
                if passed
                else f"missing text {json.dumps(missing)} tools {json.dumps(missing_tools)} :: {res.text[:120]}"
            )
            scores.append(EvalScore(id=case.id, passed=passed, detail=detail, usage=res.usage))
        except Exception as err:  # noqa: BLE001 - a throwing case scores FAIL, not crash
            scores.append(
                EvalScore(id=case.id, passed=False, detail=f"threw: {err}", usage=TokenUsage())
            )
    pass_rate = (sum(1 for s in scores if s.passed) / len(scores)) if scores else 1.0
    return EvalRun(scores=scores, pass_rate=pass_rate)


def print_report(scores: list[EvalScore], pass_rate: float) -> None:
    for s in scores:
        print(f"{'PASS' if s.passed else 'FAIL'}  {s.id}  {s.detail}")
    passed = sum(1 for s in scores if s.passed)
    print(f"\npass rate: {pass_rate * 100:.1f}% ({passed}/{len(scores)})")


__all__ = [
    "EvalCase",
    "EvalExpect",
    "EvalRun",
    "EvalScore",
    "eval_case",
    "load_jsonl",
    "print_report",
    "run_evals",
]