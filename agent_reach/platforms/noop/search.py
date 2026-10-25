"""Offline search implementation used by contract tests and examples."""

from __future__ import annotations

from typing import Any

from agent_reach.core.result import make_item


def noop_search(query: Any, *, limit: int = 3, params: Any = None, ctx: Any = None):
    count = max(0, int(limit))
    return [
        make_item(
            title=f"noop result {index + 1} for {query}",
            url=f"https://example.com/noop/{index + 1}",
            text=f"Deterministic offline result for query {query!r}.",
            raw={"query": query, "index": index},
        )
        for index in range(count)
    ]
