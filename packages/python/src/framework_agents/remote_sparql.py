"""Remote SPARQL adapter. Parity: src/semantic/remote-sparql.ts.

Talk to real SPARQL endpoints — Wikidata Query Service, Fuseki, GraphDB,
Virtuoso — with auth and pinned prefixes. The local GraphStore stays the read
path for closures; this is for data that lives OUTSIDE the agent's own graph.

Protocol: SPARQL 1.1 — SELECT via POST ``application/sparql-query``, UPDATE
via POST ``application/sparql-update``; SELECT responses must be
``application/sparql-results+json`` (asked for in Accept).
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from .errors import AgentError
from .tools import Tool, ToolContext, define_tool

#: Descriptive User-Agent by default — public endpoints (Wikidata notably)
#: REJECT requests without one with 403. No personal name anywhere.
USER_AGENT = "agent-framework/0.1 (+RemoteSparqlAdapter)"

DEFAULT_PREFIXES: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "dct": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
    "schema": "https://schema.org/",
}


@dataclass
class SparqlAuth:
    """Auth scheme for the endpoint: basic | bearer | a named header."""

    type: Literal["basic", "bearer", "header"]
    user: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None


@dataclass
class RemoteSparqlAdapter:
    """SPARQL 1.1 HTTP client with pinned prefixes, auth, and built-in retries
    for transient endpoints (429/5xx/network, Retry-After honored)."""

    endpoint: str
    update_endpoint: str | None = None
    auth: SparqlAuth | None = None
    prefixes: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    #: Injectable AsyncClient factory (tests pass httpx.MockTransport-backed).
    fetch_fn: Callable[[], httpx.AsyncClient] | None = None
    timeout_ms: float = 30_000
    #: Built-in retries for transient endpoints. Default 2. Public SPARQL
    #: endpoints rate-limit aggressively (429 with empty bodies is common).
    max_retries: int = 2

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        self.update_endpoint = (self.update_endpoint or self.endpoint).rstrip("/")
        self.prefixes = {**DEFAULT_PREFIXES, **(self.prefixes or {})}
        self.extra_headers = dict(self.headers or {})
        self.max_retries = max(0, self.max_retries)

    def with_prefixes(self, query: str) -> str:
        """Prepend pinned PREFIX declarations so caller queries stay short."""
        declarations = "\n".join(f"PREFIX {p}: <{iri}>" for p, iri in self.prefixes.items())
        return f"{declarations}\n{query}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        out = {**(extra or {}), **self.extra_headers}
        out.setdefault("user-agent", USER_AGENT)
        auth = self.auth
        if auth is None:
            return out
        if auth.type == "basic":
            token = base64.b64encode(f"{auth.user or ''}:{auth.password or ''}".encode()).decode()
            out["authorization"] = f"Basic {token}"
        elif auth.type == "bearer":
            out["authorization"] = f"Bearer {auth.token}"
        else:
            out[auth.name or "x-auth-header"] = auth.value or ""
        return out

    async def _post(self, url: str, content_type: str, body: str, accept: str) -> httpx.Response:
        """POST with retry: 429/5xx/network errors retry (honoring
        Retry-After, default backoff 2s exponential capped at 10s); a
        non-retryable 4xx fails immediately — no retry loop."""
        client = (self.fetch_fn or httpx.AsyncClient)()
        last_error: AgentError | None = None
        retry_delay: float | None = None
        try:
            for attempt in range(self.max_retries + 1):
                if attempt > 0:
                    delay = retry_delay if retry_delay is not None else min(2 * 2 ** (attempt - 1), 10)
                    await asyncio.sleep(delay)
                try:
                    res = await client.post(
                        url,
                        content=body,
                        headers=self._headers({"content-type": content_type, "accept": accept}),
                        timeout=self.timeout_ms / 1000,
                    )
                except Exception as err:  # noqa: BLE001 - any transport failure
                    last_error = AgentError("REMOTE_SPARQL", f"SPARQL request failed: {err}", retryable=True)
                    retry_delay = None  # no server hint — retry loop uses its own backoff
                    continue
                if res.status_code >= 400:
                    text = res.text
                    retryable = res.status_code == 429 or res.status_code >= 500
                    err = AgentError(
                        "REMOTE_SPARQL",
                        f"{url} HTTP {res.status_code}: {text[:300]}",
                        retryable=retryable,
                    )
                    header = res.headers.get("retry-after", "")
                    try:
                        retry_delay = min(float(header), 60) if header.strip() else None
                    except ValueError:  # HTTP-date Retry-After or garbage
                        retry_delay = None
                    if not retryable or attempt == self.max_retries:
                        raise err
                    last_error = err
                    continue
                return res
            # A network error on the final attempt lands here.
            raise last_error or AgentError("REMOTE_SPARQL", "request failed", retryable=True)
        finally:
            if hasattr(client, "aclose"):
                await client.aclose()

    async def select(self, query: str) -> list[dict[str, str]]:
        """SELECT over the remote endpoint. Returns rows of plain-string terms
        (same shape as the local execute_select) so both paths compose."""
        res = await self._post(
            self.endpoint,
            "application/sparql-query",
            self.with_prefixes(query),
            "application/sparql-results+json",
        )
        try:
            payload = res.json()
        except Exception as err:  # noqa: BLE001 - server ignored Accept
            raise AgentError(
                "REMOTE_SPARQL",
                f"Endpoint returned non-JSON results (expected sparql-results+json): {err}",
                retryable=False,
            ) from err
        head = payload.get("head") or {}
        variables = head.get("vars") or []
        bindings = (payload.get("results") or {}).get("bindings") or []
        return [
            {v: ((b.get(v) or {}).get("value") or "") for v in variables} for b in bindings
        ]

    async def update(self, query: str) -> None:
        """SPARQL 1.1 UPDATE (INSERT/DELETE DATA, DELETE WHERE...) against the
        update endpoint. Opt-in — never wire this into a read-only agent."""
        await self._post(
            self.update_endpoint,
            "application/sparql-update",
            self.with_prefixes(query),
            "*/*",
        )


class _SelectArgs(BaseModel):
    query: str = Field(min_length=1, max_length=20000)


class _SelectOrUpdateArgs(BaseModel):
    query: str = Field(min_length=1, max_length=20000)
    update: bool = False


def remote_sparql_tool(adapter: RemoteSparqlAdapter, allow_update: bool = False) -> Tool:
    """Agent-facing tool. SELECT always; UPDATE only when allow_update."""

    async def execute_select(args: _SelectArgs, ctx: ToolContext | None) -> list[dict[str, str]]:
        return await adapter.select(args.query)

    if not allow_update:
        return define_tool(
            name="remote_sparql_select",
            description="Query a remote SPARQL endpoint (read-only SELECT).",
            schema=_SelectArgs,
            execute=execute_select,
        )

    async def execute_any(args: _SelectOrUpdateArgs, ctx: ToolContext | None) -> Any:
        if args.update:
            await adapter.update(args.query)
            return {"updated": True}
        return await adapter.select(args.query)

    return define_tool(
        name="remote_sparql",
        description="Query or mutate a remote SPARQL endpoint (SELECT, or UPDATE with { update: true }).",
        schema=_SelectOrUpdateArgs,
        execute=execute_any,
    )