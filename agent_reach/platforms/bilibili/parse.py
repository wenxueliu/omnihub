"""Bilibili-specific guards around the shared web parsers.

Jina sometimes renders Bilibili's login or verification page instead of the
video. Treating that as content would silently return a page full of login
prompts, so the platform rejects it and lets the next source fail loudly.
"""

from __future__ import annotations

from typing import Any

from agent_reach.core.result import NOT_FOUND, UPSTREAM_BROKEN, ReachError
from agent_reach.parse.web import jina as _jina

_LOGIN_MARKERS = ("扫描二维码登录", "密码登录", "短信登录")


def jina(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    items = _jina(raw, action=action, ctx=ctx)
    if not items:
        return items
    item = items[0]
    title = str(item.get("title") or "")
    text = str(item.get("text") or "")[:4000]
    if "视频去哪了呢" in title:
        raise ReachError(NOT_FOUND, "Bilibili reports that the video was removed")
    if "验证码" in title or sum(marker in text for marker in _LOGIN_MARKERS) >= 2:
        raise ReachError(
            UPSTREAM_BROKEN,
            "Bilibili returned a login/verification page instead of the video",
        )
    return items
