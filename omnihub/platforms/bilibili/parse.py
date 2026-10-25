"""Turn the browser's subtitle metadata into transcript items.

The browser source resolves the video id, its title and a signed list of
subtitle URLs. The subtitle host rejects cross-origin reads from page
JavaScript, so each JSON file is downloaded here with a plain HTTP request
carrying the Bilibili Referer.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from omnihub.core.result import (
    NEED_SETUP,
    NOT_FOUND,
    UPSTREAM_BROKEN,
    OmniHubError,
    make_item,
    unix_to_iso,
)

_REFERER = "https://www.bilibili.com/"
_RAW_DIR_ENV = "OMNIHUB_RAW_DIR"


def _raw_dir() -> Path:
    override = os.environ.get(_RAW_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.cwd() / "_raw"


def _safe_name(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")[:limit].rstrip(" .-")
    # Titles are user data and may be multi-byte; keep the whole file name
    # comfortably below the common 255-byte limit.
    encoded = cleaned.encode("utf-8")
    if len(encoded) > 120:
        cleaned = encoded[:120].decode("utf-8", errors="ignore").rstrip(" .-")
    return cleaned or "bilibili"


def _srt_clock(seconds: Any) -> str:
    try:
        milliseconds = int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        milliseconds = 0
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}"


def _srt(cues: list[dict]) -> str:
    blocks = []
    for index, cue in enumerate(cues, 1):
        content = str(cue.get("content") or "").strip()
        if not content:
            continue
        start = _srt_clock(cue.get("from"))
        end = _srt_clock(cue.get("to"))
        blocks.append(f"{index}\n{start} --> {end}\n{content}\n")
    return "\n".join(blocks)


def _save(
    *,
    title: Optional[str],
    bvid: str,
    lan: str,
    cues: list[dict],
    text: Optional[str],
) -> list[str]:
    """Write the transcript next to the other raw artifacts, best effort."""
    directory = _raw_dir()
    stem = "-".join(
        (
            date.today().isoformat(),
            _safe_name(title or "bilibili"),
            _safe_name(bvid or "video", 24),
            _safe_name(lan or "subtitle", 16),
        )
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        txt_path = directory / f"{stem}.txt"
        srt_path = directory / f"{stem}.srt"
        txt_path.write_text((text or "") + "\n", encoding="utf-8")
        srt_path.write_text(_srt(cues), encoding="utf-8")
    except OSError:
        return []
    return [str(txt_path), str(srt_path)]


def _payload(raw: Any) -> dict:
    data = getattr(raw, "data", None)
    if isinstance(data, dict):
        return data
    text = str(getattr(raw, "text", "") or "")
    try:
        value = json.loads(text)
    except ValueError:
        raise OmniHubError(
            UPSTREAM_BROKEN, "Bilibili returned invalid subtitle metadata"
        ) from None
    if not isinstance(value, dict):
        raise OmniHubError(
            UPSTREAM_BROKEN, "Bilibili returned an unexpected subtitle payload"
        )
    return value


def _absolute(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return urljoin(_REFERER, url)


def _cues(ctx: Any, url: str, referer: str) -> list[dict]:
    if ctx is None:
        raise OmniHubError(NEED_SETUP, "Bilibili subtitles need an HTTP context")
    response = ctx.http(
        _absolute(url),
        headers={"Accept": "application/json", "Referer": referer},
    )
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        try:
            data = json.loads(str(getattr(response, "text", "") or "{}"))
        except ValueError:
            raise OmniHubError(
                UPSTREAM_BROKEN, "Bilibili returned an invalid subtitle file"
            ) from None
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, list):
        raise OmniHubError(UPSTREAM_BROKEN, "Bilibili subtitle file has no cues")
    return [cue for cue in body if isinstance(cue, dict)]


def _text(cues: list[dict]) -> Optional[str]:
    lines = []
    for cue in cues:
        content = str(cue.get("content") or "").strip()
        if content:
            lines.append(content)
    return "\n".join(lines) or None


def _video_url(bvid: str) -> Optional[str]:
    return f"https://www.bilibili.com/video/{bvid}/" if bvid else None


def _coverage(cues: list[dict], duration: Any) -> Optional[float]:
    try:
        end = float(cues[-1].get("to") or 0)
        total = float(duration or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0 or end <= 0:
        return None
    return end / total


def _language_key(lan: str, lan_doc: str) -> str:
    base = lan.lower()
    if "zh" in base or any(marker in lan_doc for marker in ("中文", "简体", "繁体")):
        return "zh"
    if "en" in base or "english" in lan_doc.lower() or "英文" in lan_doc:
        return "en"
    return base or lan_doc.lower() or "subtitle"


def _select(
    downloaded: list[tuple[dict, list[dict], Optional[float]]],
    duration: Any,
) -> list[tuple[dict, list[dict], Optional[float]]]:
    """Keep the track that actually covers the video, one per language.

    ``player/v2`` sometimes returns several ``ai-zh`` entries at once: the
    real track plus unrelated ones belonging to other videos or stale
    generations. A track whose cues stop far short of (or run far past) the
    video duration is not this video's subtitle.
    """
    if duration:
        full = [
            entry
            for entry in downloaded
            if entry[2] is not None and 0.8 <= entry[2] <= 1.2
        ]
        if full:
            downloaded = full
        else:
            capped = [
                entry
                for entry in downloaded
                if entry[2] is not None and entry[2] <= 1.2
            ]
            if capped:
                downloaded = capped

    best: dict[str, tuple[tuple, dict, list[dict], Optional[float]]] = {}
    order: list[str] = []
    for track, cues, coverage in downloaded:
        lan = str(track.get("lan") or "")
        lan_doc = str(track.get("lan_doc") or "")
        key = _language_key(lan, lan_doc)
        score = (
            0 if lan.lower().startswith("ai-") else 1,
            -abs((coverage if coverage is not None else 0.0) - 1.0),
            len(cues),
        )
        if key not in best:
            order.append(key)
        if key not in best or score > best[key][0]:
            best[key] = (score, track, cues, coverage)
    return [best[key][1:] for key in order]


def subtitles(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    payload = _payload(raw)
    if payload.get("error"):
        raise OmniHubError(UPSTREAM_BROKEN, f"Bilibili: {payload['error']}")

    tracks = [
        track
        for track in payload.get("subtitles") or []
        if isinstance(track, dict) and track.get("url")
    ]
    if not tracks:
        if int(payload.get("track_count") or 0):
            raise OmniHubError(
                UPSTREAM_BROKEN,
                "Bilibili listed subtitle tracks but issued no download URL",
            )
        raise OmniHubError(NOT_FOUND, "this Bilibili video has no subtitles")

    title = str(payload.get("title") or "").strip() or None
    bvid = str(payload.get("bvid") or "").strip()
    duration = payload.get("duration")
    url = _video_url(bvid)
    referer = str(getattr(raw, "meta", {}).get("page_url") or url or _REFERER)
    items: list[dict] = []
    errors: list[str] = []
    downloaded: list[tuple[dict, list[dict], Optional[float]]] = []

    for track in tracks:
        lan = str(track.get("lan") or "").strip()
        lan_doc = str(track.get("lan_doc") or lan or "").strip()
        label = lan_doc or lan or "subtitle"
        try:
            cues = _cues(ctx, str(track.get("url")), referer)
        except OmniHubError as exc:
            errors.append(f"{label}: {exc.message}")
            continue
        if not cues:
            errors.append(f"{label}: subtitle file has no cues")
            continue
        downloaded.append((track, cues, _coverage(cues, duration)))

    if not downloaded:
        raise OmniHubError(
            UPSTREAM_BROKEN,
            "Bilibili subtitles could not be downloaded: " + "; ".join(errors),
        )

    selected = _select(downloaded, duration)
    multiple = len(selected) > 1
    for track, cues, coverage in selected:
        lan = str(track.get("lan") or "").strip()
        lan_doc = str(track.get("lan_doc") or lan or "").strip()
        transcript = _text(cues)
        saved = _save(
            title=title,
            bvid=bvid,
            lan=lan,
            cues=cues,
            text=transcript,
        )
        items.append(
            make_item(
                title=f"{title}（{lan_doc}）" if multiple and title else title,
                url=url,
                author=payload.get("owner") or None,
                published_at=unix_to_iso(payload.get("pubdate")),
                text=transcript,
                raw={
                    "bvid": bvid or None,
                    "cid": payload.get("cid"),
                    "lan": lan or None,
                    "lan_doc": lan_doc or None,
                    "cues": len(cues),
                    "duration_seconds": cues[-1].get("to"),
                    "video_duration": duration,
                    "coverage": round(coverage, 3) if coverage is not None else None,
                    "saved_files": saved,
                },
            )
        )
    return items
