"""Search Bilibili videos through its WBI-signed public search API.

The unsigned ``/x/web-interface/search/type`` endpoint is answered
inconsistently (risk control drops or rejects a noticeable share of calls),
so every request is signed with the WBI keys that ``/x/web-interface/nav``
hands out. No login and no external tool are needed: a missing login only
changes ``nav.code`` to ``-101``; the keys are still returned.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode

from omnihub.core.result import (
    BAD_INPUT,
    NEED_SETUP,
    UPSTREAM_BROKEN,
    OmniHubError,
    make_item,
    unix_to_iso,
)

_API = "https://api.bilibili.com"
_REFERER = "https://www.bilibili.com/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Bilibili rotates its WBI keys roughly once a day. Reuse them for a while
#: instead of spending a request on every search.
_WBI_TTL_SECONDS = 6 * 3600
_WBI_CACHE: Optional[tuple[float, str]] = None

#: Fixed permutation published by Bilibili (``mixinKeyEncTab``).
_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

_EM_TAG = re.compile(r"</?em[^>]*>", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Referer": _REFERER,
        "Accept": "application/json",
    }


def _payload(response: Any, what: str) -> dict:
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data
    text = str(getattr(response, "text", "") or "")
    try:
        value = json.loads(text)
    except ValueError:
        raise OmniHubError(
            UPSTREAM_BROKEN, f"Bilibili {what} returned a verification page"
        ) from None
    if not isinstance(value, dict):
        raise OmniHubError(
            UPSTREAM_BROKEN, f"Bilibili {what} returned an unexpected payload"
        )
    return value


def _file_key(url: str) -> str:
    """The key part of a ``wbi_img`` URL: the file name without extension."""
    name = str(url or "").rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def mixin_key(img_key: str, sub_key: str) -> str:
    """Permute the two WBI keys into the per-day signing key."""
    ordered = img_key + sub_key
    if len(ordered) < 64:
        raise OmniHubError(UPSTREAM_BROKEN, "Bilibili returned malformed WBI keys")
    return "".join(ordered[index] for index in _MIXIN_KEY_ENC_TAB)[:32]


def sign(params: dict[str, Any], key: str, *, now: Optional[int] = None) -> str:
    """Return a signed query string, ``wts`` and ``w_rid`` included."""
    signed = {str(name): str(value) for name, value in params.items()}
    signed["wts"] = str(int(time.time() if now is None else now))
    signed = {
        name: "".join(ch for ch in value if ch not in "!'()*")
        for name, value in signed.items()
    }
    query = urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    return urlencode(sorted(signed.items()))


def _wbi_key(ctx: Any) -> str:
    global _WBI_CACHE
    cached = _WBI_CACHE
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]

    payload = _payload(ctx.http(_API + "/x/web-interface/nav", headers=_headers()), "nav")
    code = payload.get("code")
    # -101 means "not logged in"; the keys are still usable.
    if code not in (0, -101):
        raise OmniHubError(
            UPSTREAM_BROKEN,
            f"Bilibili nav API {code}: {payload.get('message') or 'unknown error'}",
        )
    images = (payload.get("data") or {}).get("wbi_img") or {}
    key = mixin_key(_file_key(images.get("img_url")), _file_key(images.get("sub_url")))
    _WBI_CACHE = (now + _WBI_TTL_SECONDS, key)
    return key


def _duration_seconds(text: Any) -> Optional[int]:
    """``"2398:14"`` / ``"1:02:03"`` -> seconds; empty or odd values -> None."""
    parts = str(text or "").strip().split(":")
    if not parts or len(parts) > 3 or not all(part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    if len(values) == 3:
        return values[0] * 3600 + values[1] * 60 + values[2]
    if len(values) == 2:
        return values[0] * 60 + values[1]
    return values[0]


def _title(text: Any) -> Optional[str]:
    cleaned = html.unescape(_EM_TAG.sub("", str(text or "")))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or None


def _text(text: Any) -> Optional[str]:
    cleaned = html.unescape(str(text or "")).replace("\r\n", "\n").strip()
    return cleaned or None


def _tags(text: Any) -> list[str]:
    return [tag.strip() for tag in str(text or "").split(",") if tag.strip()]


def _item(row: dict) -> dict:
    bvid = str(row.get("bvid") or "").strip()
    duration_text = str(row.get("duration") or "").strip() or None
    cover = str(row.get("pic") or "").strip() or None
    if cover and cover.startswith("//"):
        cover = "https:" + cover
    return make_item(
        title=_title(row.get("title")),
        url=f"https://www.bilibili.com/video/{bvid}" if bvid else None,
        author=str(row.get("author") or "").strip() or None,
        published_at=unix_to_iso(row.get("pubdate")),
        text=_text(row.get("description")),
        raw={
            "bvid": bvid or None,
            "aid": row.get("aid"),
            "mid": row.get("mid"),
            "play": row.get("play"),
            "danmaku": row.get("video_review"),
            "favorites": row.get("favorites"),
            "like": row.get("like"),
            "comments": row.get("review"),
            "duration_seconds": _duration_seconds(duration_text),
            "duration_text": duration_text,
            "pubdate": row.get("pubdate"),
            "tags": _tags(row.get("tag")),
            "category": row.get("typename") or None,
            "type": row.get("type") or None,
            "cover": cover,
        },
    )


def search(
    query: str,
    *,
    limit: int = 5,
    params: Optional[dict] = None,
    ctx: Any = None,
) -> list[dict]:
    if ctx is None:
        raise OmniHubError(NEED_SETUP, "Bilibili search needs an HTTP context")
    keyword = str(query or "").strip()
    if not keyword:
        raise OmniHubError(BAD_INPUT, "Bilibili search needs a query")

    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 5
    size = max(1, min(size, 50))

    url = (
        _API
        + "/x/web-interface/wbi/search/type?"
        + sign(
            {
                "keyword": keyword,
                "search_type": "video",
                "page": 1,
                "page_size": size,
            },
            _wbi_key(ctx),
        )
    )
    payload = _payload(ctx.http(url, headers=_headers()), "search")
    code = payload.get("code")
    if code != 0:
        message = payload.get("message") or "unknown error"
        raise OmniHubError(
            UPSTREAM_BROKEN, f"Bilibili search API {code}: {message}"
        )

    rows = (payload.get("data") or {}).get("result") or []
    # ``search_type=video`` still mixes in paid courses (``ketang``) that
    # carry no bvid, and rows without a video id cannot be read back later.
    items = [
        _item(row)
        for row in rows
        if isinstance(row, dict)
        and row.get("type") == "video"
        and str(row.get("bvid") or "").strip()
    ]
    return items[:size]
