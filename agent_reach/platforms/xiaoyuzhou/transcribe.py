"""Transcribe a Xiaoyuzhou episode through a Whisper-compatible API.

The episode page embeds its public audio URL in ``__NEXT_DATA__``; paid and
private episodes are rejected before anything is downloaded. A direct public
audio URL is also accepted so a previously downloaded file can be reused.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from agent_reach.core.result import (
    BAD_INPUT,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)
from agent_reach.transcribe import transcribe_audio

_XIAOYUZHOU_HOSTS = frozenset({"xiaoyuzhoufm.com", "www.xiaoyuzhoufm.com"})
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_ENCLOSURE_RE = re.compile(r'"enclosure":\{"url":"((?:\\.|[^"\\])*)"')
_AUDIO_RE = re.compile(
    r'https://media\.xyzcdn\.net/[^"\\\s]+?\.(?:m4a|mp3|mp4|aac)(?:\?[^"\\\s]*)?',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r'"title":"((?:\\.|[^"\\])*)"')
_DURATION_RE = re.compile(r'"duration":(\d+)')
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.IGNORECASE
)
_FREE_PAY_TYPES = frozenset({"FREE", "FREE_TRIAL"})


def _json_string(value: str) -> str:
    try:
        return str(json.loads(f'"{value}"'))
    except (ValueError, TypeError):
        return value.replace("\\/", "/")


def _next_episode(html: str) -> Optional[dict[str, Any]]:
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    episode = (((payload.get("props") or {}).get("pageProps") or {}).get("episode"))
    return episode if isinstance(episode, dict) else None


def _episode_from_next_data(episode: dict[str, Any]) -> dict[str, Any]:
    if episode.get("isPrivateMedia"):
        raise ReachError(BAD_INPUT, "episode audio is private")

    pay_type = str(episode.get("payType") or "FREE").upper()
    if pay_type not in _FREE_PAY_TYPES:
        raise ReachError(BAD_INPUT, f"episode is not free to transcribe ({pay_type})")

    audio_url = str((episode.get("enclosure") or {}).get("url") or "").strip()
    if not audio_url:
        media = episode.get("media") or {}
        source = media.get("source") or {}
        if str(source.get("mode") or "PUBLIC").upper() != "PUBLIC":
            raise ReachError(BAD_INPUT, "episode media is not public")
        audio_url = str(source.get("url") or "").strip()
    if not audio_url:
        raise ReachError(UPSTREAM_BROKEN, "could not find an audio URL on the episode page")

    podcast = episode.get("podcast") or {}
    duration = episode.get("duration")
    try:
        duration_seconds: Optional[int] = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None

    return {
        "audio_url": audio_url,
        "title": str(episode.get("title") or "").strip() or None,
        "podcast": str(podcast.get("title") or "").strip() or None,
        "episode_id": str(episode.get("eid") or "").strip() or None,
        "duration_seconds": duration_seconds,
        "published_at": str(episode.get("pubDate") or "").strip() or None,
    }


def _episode_from_html(html: str) -> dict[str, Any]:
    audio_match = _ENCLOSURE_RE.search(html)
    audio_url = _json_string(audio_match.group(1)) if audio_match else None
    if not audio_url:
        audio_match = _AUDIO_RE.search(html)
        audio_url = audio_match.group(0) if audio_match else None
    if not audio_url:
        raise ReachError(UPSTREAM_BROKEN, "could not find an audio URL on the episode page")

    title_match = _OG_TITLE_RE.search(html) or _TITLE_RE.search(html)
    duration_match = _DURATION_RE.search(html)
    try:
        duration_seconds = int(duration_match.group(1)) if duration_match else None
    except (TypeError, ValueError):
        duration_seconds = None

    return {
        "audio_url": audio_url,
        "title": _json_string(title_match.group(1)).strip() if title_match else None,
        "podcast": None,
        "episode_id": None,
        "duration_seconds": duration_seconds,
        "published_at": None,
    }


def extract_episode(html: str) -> dict[str, Any]:
    """Extract the audio URL and metadata from a Xiaoyuzhou episode page."""
    episode = _next_episode(html)
    if episode is not None:
        return _episode_from_next_data(episode)
    return _episode_from_html(html)


def _is_xiaoyuzhou(target: str) -> bool:
    host = (urlsplit(target).hostname or "").lower().rstrip(".")
    return host in _XIAOYUZHOU_HOSTS or host.endswith(".xiaoyuzhoufm.com")


def _fetch_page(target: str, ctx: Any) -> str:
    raw = ctx.http(target)
    text = str(getattr(raw, "text", "") or "")
    if not text.strip():
        raise ReachError(UPSTREAM_BROKEN, "Xiaoyuzhou returned an empty episode page")
    return text


def transcribe(*, params: Any = None, ctx: Any = None) -> list[dict]:
    """Builtin ``transcribe`` action for the xiaoyuzhou platform."""
    params = dict(params or {})
    target = str(params.get("target") or "").strip()
    if not target:
        raise ReachError(BAD_INPUT, "transcribe requires an episode URL")

    parts = urlsplit(target)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ReachError(BAD_INPUT, "transcribe requires an http(s) episode or audio URL")

    if _is_xiaoyuzhou(target):
        episode = extract_episode(_fetch_page(target, ctx))
    else:
        # A direct public audio URL (for example the CDN link from a previous
        # run) skips page parsing and goes straight to the downloader.
        episode = {
            "audio_url": target,
            "title": None,
            "podcast": None,
            "episode_id": None,
            "duration_seconds": None,
            "published_at": None,
        }

    result = transcribe_audio(
        str(episode["audio_url"]),
        ctx=ctx,
        provider=str(params.get("provider") or "auto"),
        model=params.get("model") or None,
        language=str(params.get("language") or "zh"),
        polish=bool(params.get("polish")),
    )

    raw = {
        "podcast": episode.get("podcast"),
        "episode_id": episode.get("episode_id"),
        "page_duration_seconds": episode.get("duration_seconds"),
        "provider": result["provider"],
        "model": result["model"],
        "language": result["language"],
        "duration_seconds": result["duration_seconds"],
        "chunks": result["chunks"],
        "polished": result["polished"],
        "polish_error": result["polish_error"],
    }
    return [
        make_item(
            title=episode.get("title"),
            url=target,
            author=episode.get("podcast"),
            published_at=episode.get("published_at"),
            text=result["text"],
            raw=raw,
        )
    ]
