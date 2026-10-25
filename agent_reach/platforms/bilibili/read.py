"""Read a Bilibili video's metadata through the web API.

This source only runs when ``BILIBILI_COOKIE`` is configured; without a cookie
the public API rejects anonymous requests with 412. The cookie is sent as an
HTTP header and never appears in items or errors.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from agent_reach.core.result import (
    BAD_INPUT,
    NOT_FOUND,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
    unix_to_iso,
)

_API = "https://api.bilibili.com/x/web-interface/view"
_BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")


def _bvid(target: Any) -> Optional[str]:
    match = _BVID_RE.search(str(target or ""))
    return match.group(1) if match else None


def _json(ctx: Any, url: str, headers: dict[str, str]) -> dict:
    raw = ctx.http(url, headers=headers)
    data = getattr(raw, "data", None)
    if isinstance(data, dict):
        return data
    text = str(getattr(raw, "text", "") or "")
    try:
        value = json.loads(text)
    except ValueError:
        raise ReachError(UPSTREAM_BROKEN, "Bilibili returned invalid JSON") from None
    if not isinstance(value, dict):
        raise ReachError(UPSTREAM_BROKEN, "Bilibili returned an unexpected payload")
    return value


def _text(description: str, pages: Any) -> Optional[str]:
    blocks: list[str] = []
    if description:
        blocks.append(description)
    if isinstance(pages, list) and pages:
        lines = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            number = page.get("page")
            title = str(page.get("part") or "").strip()
            if not title:
                continue
            duration = page.get("duration")
            suffix = f" ({duration}s)" if duration else ""
            lines.append(f"{number}. {title}{suffix}")
        if lines:
            blocks.append("## Pages\n\n" + "\n".join(lines))
    return "\n\n".join(blocks) or None


def read_video(target: Any, *, params: Any = None, ctx: Any = None) -> list[dict]:
    bvid = _bvid(target)
    if bvid is None:
        # b23.tv short links and non-video pages fall back to the web readers.
        return []
    cookie = ctx.secret("BILIBILI_COOKIE")
    if not cookie:
        return []

    payload = _json(
        ctx,
        f"{_API}?bvid={bvid}",
        headers={
            "Accept": "application/json",
            "Cookie": str(cookie),
            "Referer": f"https://www.bilibili.com/video/{bvid}/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        },
    )
    code = payload.get("code")
    if code == -404:
        raise ReachError(NOT_FOUND, f"Bilibili video {bvid} does not exist")
    if code == -400:
        raise ReachError(BAD_INPUT, f"Bilibili rejected video id {bvid}")
    if code != 0:
        message = str(payload.get("message") or "unknown error")
        raise ReachError(UPSTREAM_BROKEN, f"Bilibili API {code}: {message}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ReachError(UPSTREAM_BROKEN, "Bilibili returned no video data")
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    pages = data.get("pages")
    description = str(data.get("desc") or "").strip()
    return [
        make_item(
            title=data.get("title"),
            url=data.get("short_link_v2")
            or f"https://www.bilibili.com/video/{data.get('bvid') or bvid}/",
            author=owner.get("name"),
            published_at=unix_to_iso(data.get("pubdate")),
            text=_text(description, pages),
            raw={
                "bvid": data.get("bvid") or bvid,
                "aid": data.get("aid"),
                "duration": data.get("duration"),
                "pages": len(pages) if isinstance(pages, list) else None,
                "copyright": data.get("copyright"),
                "stats": {
                    key: stat.get(key)
                    for key in ("view", "danmaku", "reply", "favorite", "coin", "share", "like")
                    if key in stat
                },
            },
        )
    ]
