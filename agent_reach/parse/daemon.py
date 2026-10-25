"""Parser for the daemon adapter's composite ``read`` result.

``read`` returns ``{url, title, text, truncated, truncated_reason, pages}``.
The page text is already accessibility-tree text, so the only job here is to
shape it into an item and refuse an empty page instead of reporting success
with no content.
"""

from __future__ import annotations

from typing import Any

from agent_reach.core.result import UPSTREAM_BROKEN, ReachError, make_item


def daemon_page(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    data = getattr(raw, "data", None)
    if not isinstance(data, dict):
        raise ReachError(UPSTREAM_BROKEN, "browser reader returned no structured page")
    text = str(data.get("text") or "").strip()
    if not text:
        raise ReachError(UPSTREAM_BROKEN, "browser reader returned an empty page")
    params = getattr(raw, "meta", {}).get("params") or {}
    return [
        make_item(
            title=data.get("title"),
            url=data.get("url") or params.get("target"),
            text=text,
            raw={
                "pages": data.get("pages"),
                "truncated": data.get("truncated"),
                "truncated_reason": data.get("truncated_reason"),
            },
        )
    ]
