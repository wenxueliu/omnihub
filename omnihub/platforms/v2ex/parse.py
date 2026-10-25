"""SOV2EX search-result parser."""

from __future__ import annotations

import json
import re
from typing import Any

from omnihub.core.result import UPSTREAM_BROKEN, OmniHubError, make_item


def _payload(raw: Any) -> dict:
    data = getattr(raw, "data", None)
    if isinstance(data, dict):
        return data
    text = str(getattr(raw, "text", "") or "")
    try:
        value = json.loads(text)
    except ValueError:
        raise OmniHubError(UPSTREAM_BROKEN, "SOV2EX returned invalid JSON") from None
    if not isinstance(value, dict):
        raise OmniHubError(UPSTREAM_BROKEN, "SOV2EX returned an unexpected payload")
    return value


def _limit(raw: Any, default: int = 5) -> int:
    params = getattr(raw, "meta", {}).get("params") or {}
    try:
        return max(0, int(params.get("limit", default)))
    except (TypeError, ValueError):
        return default


_EMPHASIS_RE = re.compile(r"</?em>", re.IGNORECASE)


def _snippet(hit: dict) -> str | None:
    highlight = hit.get("highlight") or {}
    if isinstance(highlight, dict):
        pieces = highlight.get("content") or []
        if isinstance(pieces, list):
            joined = " … ".join(str(piece) for piece in pieces if piece)
            if joined:
                return _EMPHASIS_RE.sub("", joined)
    content = (hit.get("_source") or {}).get("content")
    if not content:
        return None
    text = str(content).strip()
    return text[:500] + ("…" if len(text) > 500 else "")


def v2ex_search(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    payload = _payload(raw)
    hits = payload.get("hits")
    if not isinstance(hits, list):
        raise OmniHubError(UPSTREAM_BROKEN, "SOV2EX returned no search hits")

    items: list[dict] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source") or {}
        if not isinstance(source, dict):
            continue
        topic_id = source.get("id") or hit.get("_id")
        if topic_id is None:
            continue
        items.append(
            make_item(
                title=source.get("title"),
                url=f"https://www.v2ex.com/t/{topic_id}",
                author=source.get("member"),
                published_at=source.get("created"),
                text=_snippet(hit),
                raw={
                    "id": topic_id,
                    "node": source.get("node"),
                    "replies": source.get("replies"),
                    "created": source.get("created"),
                },
            )
        )
    return items[: _limit(raw)]
