"""Offline read implementation used by contract tests and examples."""

from __future__ import annotations

from typing import Any

from omnihub.core.result import make_item


def noop_read(target: Any, *, params: Any = None, ctx: Any = None):
    return [
        make_item(
            title="noop read",
            url=target,
            text=f"Deterministic offline content for {target!r}.",
            raw={"target": target},
        )
    ]
