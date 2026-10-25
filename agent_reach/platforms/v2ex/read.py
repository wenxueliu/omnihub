"""Read a V2EX topic and its first page of replies through the public API."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from agent_reach.core.result import (
    NOT_FOUND,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
    unix_to_iso,
)

_API = "https://www.v2ex.com/api"
_URL_RE = re.compile(r"^https?://(?:www\.)?v2ex\.com/t/(?P<id>\d+)", re.IGNORECASE)
_ID_RE = re.compile(r"^(?:t/)?(?P<id>\d+)$")
_HEADERS = {"Accept": "application/json", "User-Agent": "reach-platforms/0.1"}


def _topic_id(target: Any) -> Optional[int]:
    value = str(target or "").strip()
    match = _URL_RE.match(value) or _ID_RE.match(value)
    if not match:
        return None
    try:
        return int(match.group("id"))
    except (TypeError, ValueError):
        return None


def _json(ctx: Any, url: str) -> Any:
    raw = ctx.http(url, headers=_HEADERS)
    data = getattr(raw, "data", None)
    if isinstance(data, (dict, list)):
        return data
    text = str(getattr(raw, "text", "") or "")
    try:
        return json.loads(text)
    except ValueError:
        raise ReachError(UPSTREAM_BROKEN, "V2EX returned invalid JSON") from None


def _username(member: Any) -> Optional[str]:
    if isinstance(member, dict):
        return member.get("username")
    return None


def _reply_lines(replies: Any) -> list[str]:
    lines: list[str] = []
    if not isinstance(replies, list):
        return lines
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        body = str(reply.get("content") or "").strip()
        if not body:
            continue
        author = _username(reply.get("member")) or "unknown"
        lines.append(f"**@{author}** ({unix_to_iso(reply.get('created')) or ''}):\n{body}")
    return lines


def read_topic(target: Any, *, params: Any = None, ctx: Any = None) -> list[dict]:
    topic_id = _topic_id(target)
    if topic_id is None:
        # Not a topic: let another platform or the web reader try.
        return []

    data = _json(ctx, f"{_API}/topics/show.json?id={topic_id}")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ReachError(NOT_FOUND, f"V2EX topic {topic_id} does not exist")
    topic = data[0]

    replies: Any = []
    try:
        replies = _json(ctx, f"{_API}/replies/show.json?topic_id={topic_id}&page=1")
    except ReachError as exc:
        # Replies are supplementary; only a missing topic should abort.
        if exc.code != NOT_FOUND:
            raise
    lines = _reply_lines(replies)
    body = str(topic.get("content") or "").strip()
    comments = "\n\n".join(lines)
    text = body
    if comments:
        text = f"{body}\n\n---\n\n## Replies\n\n{comments}".strip()

    node = topic.get("node")
    return [
        make_item(
            title=topic.get("title"),
            url=topic.get("url") or f"https://www.v2ex.com/t/{topic_id}",
            author=_username(topic.get("member")),
            published_at=unix_to_iso(topic.get("created")),
            text=text or None,
            raw={
                "id": topic.get("id") or topic_id,
                "node": (node or {}).get("name") if isinstance(node, dict) else None,
                "node_title": (node or {}).get("title") if isinstance(node, dict) else None,
                "replies": topic.get("replies"),
                "replies_read": len(lines),
                "last_touched": unix_to_iso(topic.get("last_touched")),
            },
        )
    ]
