"""Retrieval interface — no bundled vector DB (local-first).

Parity: packages/typescript/src/rag/index.ts. InMemoryRetriever is a
dependency-free TF-IDF cosine retriever (idf in BOTH norms, as TS's
so-calculated variant double-weights matching terms); retrieval_tool()
exposes any Retriever to agents via define_tool.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Protocol, Optional, runtime_checkable

from pydantic import BaseModel, Field, create_model

from .skills import tokenize
from .tools import Tool, ToolContext, ToolResult, define_tool


@dataclass
class ScoredDoc:
    id: str
    text: str
    score: float
    meta: Optional[dict[str, Any]] = None


@runtime_checkable
class Retriever(Protocol):
    id: str

    def search(self, query: str, k: int = 4) -> Awaitable[list[ScoredDoc]]: ...


def _doc_fields(doc: Any) -> tuple[str, str, Optional[dict[str, Any]]]:
    if isinstance(doc, dict):
        return doc.get("id", ""), doc.get("text", ""), doc.get("meta")
    return doc.id, doc.text, getattr(doc, "meta", None)


class InMemoryRetriever:
    """Dependency-free TF-IDF retriever over static documents."""

    id = "tfidf"

    def __init__(self, docs: list[Any]) -> None:
        self._docs: list[tuple[str, str, Optional[dict[str, Any]], dict[str, int]]] = []
        df: dict[str, int] = {}
        for doc in docs:
            doc_id, text, meta = _doc_fields(doc)
            terms: dict[str, int] = {}
            for term in tokenize(text):
                terms[term] = terms.get(term, 0) + 1
            self._docs.append((doc_id, text, meta, terms))
        for _, _, _, terms in self._docs:
            for term in terms:
                df[term] = df.get(term, 0) + 1
        self._df = df

    def _idf(self, term: str) -> float:
        return math.log((len(self._docs) + 1) / (self._df.get(term, 0) + 1)) + 1

    async def search(self, query: str, k: int = 4) -> list[ScoredDoc]:
        qtf: dict[str, int] = {}
        for term in tokenize(query):
            qtf[term] = qtf.get(term, 0) + 1
        results: list[ScoredDoc] = []
        for doc_id, text, meta, terms in self._docs:
            dot = 0.0
            dnorm = 0.0
            for term, count in terms.items():
                weight = count * self._idf(term)
                dnorm += weight * weight
                if term in qtf:
                    dot += weight * qtf[term] * self._idf(term)
            # idf weights BOTH norms: query side too (TS's so-calculated
            # cosine double-counts idf on the shared terms; keeping the
            # weighting symmetric preserves its ranking).
            qnorm = math.sqrt(sum((count * self._idf(term)) ** 2 for term, count in qtf.items())) or 1.0
            denom = math.sqrt(dnorm) * qnorm
            if not denom:
                continue
            results.append(ScoredDoc(id=doc_id, text=text, score=dot / denom, meta=meta))
        return [d for d in sorted(results, key=lambda d: d.score, reverse=True) if d.score > 0][:k]


def _retrieve_args_schema(default_k: int) -> type[BaseModel]:
    """Per-tool arg schema with the caller's k baked in as the default."""
    return create_model(
        "RetrieveArgs",
        query=(str, Field(min_length=1, max_length=500)),
        k=(int, Field(ge=1, le=10, default=default_k)),
    )


def retrieval_tool(retriever: Retriever, name: str = "retrieve", k: int = 4) -> Tool:
    """Expose a Retriever to agents. Returns top passages with scores."""
    args_schema = _retrieve_args_schema(k)

    async def execute(args: Any, ctx: ToolContext | None = None) -> list[ScoredDoc]:
        return await retriever.search(args.query, args.k)

    async def to_result(output: Any) -> ToolResult:
        payload = [vars(d) for d in output]
        return ToolResult(content=json.dumps(payload, default=str))

    return define_tool(
        name=name,
        description=f"Search the knowledge base ({retriever.id}). Returns top passages with scores.",
        schema=args_schema,
        execute=execute,
        to_result=to_result,
    )


__all__ = [
    "InMemoryRetriever",
    "Retriever",
    "ScoredDoc",
    "retrieval_tool",
]