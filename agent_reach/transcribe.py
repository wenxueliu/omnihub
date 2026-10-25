"""Whisper-compatible audio transcription shared by platform hooks.

Downloads a public audio URL, compresses and chunks it with ``ffmpeg``, and
posts every chunk to a Whisper-compatible endpoint (Groq or OpenAI). Optional
polishing asks Groq's Llama to add Chinese punctuation without changing words.

The module uses ``requests`` directly for streaming downloads and multipart
uploads because the shared HTTP context buffers whole bodies (audio files can
be larger than its 64 MiB ceiling). Public-URL checks still come from the
shared policy module, and provider keys come from ``ctx.secret`` so they are
never written to items or error messages.

Entry point:
    transcribe_audio(url, ctx=..., provider="auto", language="zh", polish=False)
"""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests

from agent_reach.core.context import CommandFailed
from agent_reach.core.policy import ensure_public_resolution, ensure_public_url
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_SETUP,
    NOT_FOUND,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
    ReachError,
)

# Whisper's upload limit is 25 MB; leave headroom for multipart overhead.
SIZE_LIMIT_BYTES = 24 * 1024 * 1024
CHUNK_SECONDS = 600  # 10 minutes — small enough that cuts rarely lose meaning
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_CHUNKS = 24  # four hours at the standard 10-minute segment size
MAX_TOTAL_CHUNK_BYTES = 96 * 1024 * 1024
MAX_AUDIO_SECONDS = MAX_CHUNKS * CHUNK_SECONDS

DOWNLOAD_TIMEOUT = (15.0, 120.0)
DOWNLOAD_DEADLINE_SECONDS = 1800.0
TRANSCRIBE_TIMEOUT = 600.0
PROBE_TIMEOUT = 60.0
FFMPEG_TIMEOUT = 300.0
POLISH_TIMEOUT = 180.0
POLISH_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
POLISH_MODEL = "llama-3.3-70b-versatile"
POLISH_MAX_DEPTH = 3
USER_AGENT = "reach-platforms/0.1 (+https://github.com/)"
_MAX_REDIRECTS = 5
_ALLOWED_SUFFIXES = frozenset({".m4a", ".mp3", ".mp4", ".wav", ".aac", ".ogg", ".webm"})
_CHAT_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

PROVIDERS: dict[str, dict[str, Any]] = {
    "groq": {
        "endpoint": "https://api.groq.com/openai/v1/audio/transcriptions",
        "model": "whisper-large-v3",
        "secrets": ("GROQ_API_KEY", "groq_api_key"),
    },
    "openai": {
        "endpoint": "https://api.openai.com/v1/audio/transcriptions",
        "model": "whisper-1",
        "secrets": ("OPENAI_API_KEY", "openai_api_key"),
    },
}
DEFAULT_ORDER = ("groq", "openai")

_ZH_PROMPT = (
    "以下是一段中文普通话播客录音，请输出包含完整中文标点"
    "（，。？！：；“”‘’）的转写文本。"
)
_POLISH_PROMPT = (
    "以下是一段中文普通话播客的语音转写片段，由于 Whisper 对中文标点支持较弱，"
    "整段几乎没有标点。请你只做一件事：在合适位置补充中文标点（，。！？：；），"
    "可以适度分段。\n\n"
    "严格要求：\n"
    "- 不得修改、删除、增加任何汉字或英文/数字\n"
    "- 不得改写、润色、总结\n"
    "- 不得添加任何解释、前言、后记\n"
    "- 直接输出加好标点+合理分段后的全文\n\n"
    "原文：\n{text}"
)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


def _provider_key(ctx: Any, provider: str) -> Optional[str]:
    for name in PROVIDERS[provider]["secrets"]:
        value = ctx.secret(name) if ctx is not None else os.environ.get(name)
        if value:
            return str(value)
    return None


def resolve_provider(
    provider: str,
    ctx: Any,
    *,
    model: Optional[str] = None,
) -> tuple[str, str, str]:
    """Return ``(provider, model, key)`` or fail before any expensive work.

    ``auto`` picks the first configured provider in ``DEFAULT_ORDER`` and never
    falls back to a second provider implicitly: audio is sent only to the
    service the caller selected.
    """
    name = (provider or "auto").strip().lower()
    if name != "auto" and name not in PROVIDERS:
        allowed = ", ".join(("auto",) + DEFAULT_ORDER)
        raise ReachError(BAD_INPUT, f"unknown provider {name!r}; use {allowed}")

    candidates = DEFAULT_ORDER if name == "auto" else (name,)
    for candidate in candidates:
        key = _provider_key(ctx, candidate)
        if key:
            return candidate, str(model or PROVIDERS[candidate]["model"]), key

    names = ", ".join(PROVIDERS[candidate]["secrets"][0] for candidate in candidates)
    raise ReachError(
        NEED_SETUP,
        f"no transcription API key configured (need one of: {names})",
        hint=(
            "Set the key in the environment or ~/.agent-reach/secrets.yaml. "
            "Groq offers a free tier at https://console.groq.com."
        ),
    )


def _retry_after(response: requests.Response) -> Optional[float]:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def transcribe_chunk(
    chunk: Path,
    *,
    provider: str,
    key: str,
    model: str,
    language: str = "",
    prompt: str = "",
    timeout: float = TRANSCRIBE_TIMEOUT,
) -> str:
    """Transcribe one audio chunk via a Whisper-compatible endpoint."""
    info = PROVIDERS[provider]
    data = {"model": model, "response_format": "text"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt

    try:
        with Path(chunk).open("rb") as handle:
            response = requests.post(
                info["endpoint"],
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (Path(chunk).name, handle, "audio/m4a")},
                data=data,
                timeout=timeout,
            )
    except requests.Timeout:
        raise ReachError(
            TIMEOUT, f"{provider}: transcription request timed out"
        ) from None
    except requests.RequestException as exc:
        raise ReachError(
            UPSTREAM_BROKEN, f"{provider}: network error: {exc}"
        ) from None

    if response.status_code == 429:
        raise ReachError(
            RATE_LIMITED,
            f"{provider}: rate limited",
            retry_after_seconds=_retry_after(response),
        )
    if response.status_code in {401, 403}:
        raise ReachError(
            NEED_SETUP,
            f"{provider}: the API key was rejected (HTTP {response.status_code})",
            hint=f"Check {PROVIDERS[provider]['secrets'][0]} and try again.",
        )
    if not response.ok:
        detail = (response.text or "").strip()[:200]
        raise ReachError(
            UPSTREAM_BROKEN,
            f"{provider}: HTTP {response.status_code}: {detail or 'upstream error'}",
        )
    return (response.text or "").strip()


# ---------------------------------------------------------------------------
# optional punctuation polishing
# ---------------------------------------------------------------------------


def _polish_once(text: str, key: str) -> tuple[str, Optional[str]]:
    body = {
        "model": POLISH_MODEL,
        "temperature": 0.2,
        "max_completion_tokens": 8192,
        "messages": [
            {"role": "user", "content": _POLISH_PROMPT.replace("{text}", text)}
        ],
    }
    try:
        response = requests.post(
            _CHAT_ENDPOINT,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            json=body,
            timeout=POLISH_TIMEOUT,
        )
    except requests.Timeout:
        raise ReachError(TIMEOUT, "polish request timed out") from None
    except requests.RequestException as exc:
        raise ReachError(UPSTREAM_BROKEN, f"polish network error: {exc}") from None

    if response.status_code == 429:
        raise ReachError(
            RATE_LIMITED,
            "polish: rate limited",
            retry_after_seconds=_retry_after(response),
        )
    if not response.ok:
        raise ReachError(
            UPSTREAM_BROKEN,
            f"polish: HTTP {response.status_code}: {(response.text or '').strip()[:200]}",
        )
    if len(response.content or b"") > POLISH_MAX_RESPONSE_BYTES:
        raise ReachError(UPSTREAM_BROKEN, "polish response exceeded the size limit")
    try:
        payload = response.json()
        choice = payload["choices"][0]
        content = str(choice["message"]["content"]).strip()
        finish_reason = choice.get("finish_reason")
    except (ValueError, KeyError, IndexError, TypeError):
        raise ReachError(UPSTREAM_BROKEN, "polish returned an unexpected payload") from None
    return content, finish_reason


def polish_text(text: str, key: str) -> tuple[str, Optional[str]]:
    """Add punctuation/paragraphs; return the original text plus any error."""

    def run(piece: str, depth: int) -> str:
        content, finish_reason = _polish_once(piece, key)
        if not content.strip():
            return piece
        if finish_reason != "length" or depth >= POLISH_MAX_DEPTH:
            return content
        midpoint = len(piece) // 2
        return run(piece[:midpoint], depth + 1) + run(piece[midpoint:], depth + 1)

    if not text.strip():
        return text, None
    try:
        return run(text, 0), None
    except ReachError as exc:
        return text, exc.message


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ReachError(
            NEED_SETUP,
            f"{name} is required for audio transcription but was not found on PATH",
            hint=f"Install {name} (for example `apt install ffmpeg` or `brew install ffmpeg`).",
        )
    return path


def _run_ffmpeg(ctx: Any, argv: list[str], timeout: float) -> Any:
    try:
        return ctx.run(argv, timeout=timeout)
    except CommandFailed as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[:300]
        raise ReachError(
            UPSTREAM_BROKEN,
            f"ffmpeg failed: {detail or f'exit code {exc.returncode}'}",
        ) from None
    except ReachError:
        raise


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def parse_duration(text: str) -> Optional[float]:
    """Extract the duration ffmpeg prints for its input."""
    match = _DURATION_RE.search(text or "")
    if not match:
        return None
    seconds = (
        int(match.group(1)) * 3600
        + int(match.group(2)) * 60
        + float(match.group(3))
    )
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def probe_duration(ctx: Any, path: Path) -> float:
    """Read duration with ``ffmpeg -i`` (no ffprobe dependency).

    Without an output argument ffmpeg exits non-zero after printing the input
    details, so ``CommandFailed`` carries the text we need.
    """
    ffmpeg = require_tool("ffmpeg")
    argv = [ffmpeg, "-hide_banner", "-i", str(path)]
    try:
        result = ctx.run(argv, timeout=PROBE_TIMEOUT)
        stderr = getattr(result, "stderr", "")
    except CommandFailed as exc:
        stderr = exc.stderr
    seconds = parse_duration(stderr)
    if seconds is None:
        raise ReachError(
            UPSTREAM_BROKEN, "ffmpeg could not read the audio duration"
        )
    if seconds > MAX_AUDIO_SECONDS:
        raise ReachError(
            BAD_INPUT,
            f"audio is longer than the {MAX_AUDIO_SECONDS // 60}-minute limit",
        )
    return seconds


def compress_audio(ctx: Any, source: Path, directory: Path) -> Path:
    """Re-encode to mono / 16 kHz / 32 kbps m4a — most episodes fit in 25 MB."""
    ffmpeg = require_tool("ffmpeg")
    destination = directory / "compressed.m4a"
    _run_ffmpeg(
        ctx,
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            str(MAX_AUDIO_SECONDS),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            str(destination),
        ],
        FFMPEG_TIMEOUT,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ReachError(UPSTREAM_BROKEN, "ffmpeg produced no compressed audio")
    return destination


def chunk_audio(
    ctx: Any,
    source: Path,
    directory: Path,
    segment_seconds: int = CHUNK_SECONDS,
) -> list[Path]:
    """Split compressed audio into upload-sized segments."""
    if segment_seconds <= 0:
        raise ReachError(BAD_INPUT, "chunk segment duration must be positive")
    possible = (MAX_AUDIO_SECONDS + segment_seconds - 1) // segment_seconds
    if possible > MAX_CHUNKS:
        raise ReachError(
            BAD_INPUT,
            f"segment duration {segment_seconds}s could create {possible} chunks; "
            f"the limit is {MAX_CHUNKS}",
        )
    ffmpeg = require_tool("ffmpeg")
    pattern = directory / "chunk_%03d.m4a"
    _run_ffmpeg(
        ctx,
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            str(MAX_AUDIO_SECONDS),
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            str(pattern),
        ],
        FFMPEG_TIMEOUT,
    )
    chunks = sorted(directory.glob("chunk_*.m4a"))
    if not chunks:
        raise ReachError(UPSTREAM_BROKEN, "ffmpeg produced no audio chunks")
    return chunks


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def _checked_public_url(url: str) -> str:
    current = ensure_public_url(url)
    ensure_public_resolution(current)
    return current


def _audio_suffix(url: str) -> str:
    suffix = Path(unquote(urlsplit(url).path)).suffix.lower()
    return suffix if suffix in _ALLOWED_SUFFIXES else ".audio"


def download_audio(
    url: str,
    directory: Path,
    *,
    user_agent: str = USER_AGENT,
    timeout: tuple[float, float] = DOWNLOAD_TIMEOUT,
    deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
) -> Path:
    """Stream a public audio URL into ``directory`` with size/time limits."""
    current = _checked_public_url(url)
    headers = {"User-Agent": user_agent}
    deadline = time.monotonic() + deadline_seconds
    response: Optional[requests.Response] = None

    for _ in range(_MAX_REDIRECTS + 1):
        try:
            response = requests.get(
                current,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=timeout,
            )
        except requests.Timeout:
            raise ReachError(TIMEOUT, "audio download timed out") from None
        except requests.RequestException as exc:
            raise ReachError(UPSTREAM_BROKEN, f"audio download failed: {exc}") from None

        location = response.headers.get("Location")
        if response.status_code in {301, 302, 303, 307, 308} and location:
            response.close()
            current = _checked_public_url(urljoin(current, location))
            continue
        break
    else:
        if response is not None:
            response.close()
        raise ReachError(UPSTREAM_BROKEN, "too many redirects while downloading audio")

    assert response is not None
    with response:
        if response.status_code == 404:
            raise ReachError(NOT_FOUND, "audio URL returned HTTP 404")
        if response.status_code == 429:
            raise ReachError(
                RATE_LIMITED,
                "audio CDN returned HTTP 429",
                retry_after_seconds=_retry_after(response),
            )
        if response.status_code >= 400:
            raise ReachError(
                UPSTREAM_BROKEN,
                f"audio download failed: HTTP {response.status_code}",
            )

        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_SOURCE_BYTES:
            limit_mib = MAX_SOURCE_BYTES // (1024 * 1024)
            raise ReachError(BAD_INPUT, f"audio exceeds the {limit_mib} MiB source limit")

        destination = Path(directory) / f"source{_audio_suffix(current)}"
        written = 0
        with destination.open("wb") as handle:
            for block in response.iter_content(chunk_size=256 * 1024):
                if not block:
                    continue
                written += len(block)
                if written > MAX_SOURCE_BYTES:
                    limit_mib = MAX_SOURCE_BYTES // (1024 * 1024)
                    raise ReachError(
                        BAD_INPUT, f"audio exceeds the {limit_mib} MiB source limit"
                    )
                if time.monotonic() > deadline:
                    raise ReachError(
                        TIMEOUT,
                        f"audio download exceeded {int(deadline_seconds)}s",
                    )
                handle.write(block)

    if written == 0:
        raise ReachError(UPSTREAM_BROKEN, "downloaded audio is empty")
    return destination


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _language_prompt(language: str) -> str:
    return _ZH_PROMPT if (language or "").lower().startswith("zh") else ""


def transcribe_audio(
    url: str,
    *,
    ctx: Any = None,
    provider: str = "auto",
    model: Optional[str] = None,
    language: str = "zh",
    polish: bool = False,
) -> dict[str, Any]:
    """Download ``url`` and return the transcript plus run metadata."""
    provider_name, model_name, key = resolve_provider(provider, ctx, model=model)
    polish_key: Optional[str] = None
    if polish:
        polish_key = _provider_key(ctx, "groq")
        if not polish_key:
            raise ReachError(
                NEED_SETUP,
                "polish requires GROQ_API_KEY",
                hint="Set GROQ_API_KEY or drop polish to keep the raw transcript.",
            )

    with tempfile.TemporaryDirectory(prefix="reach-transcribe-") as raw_dir:
        work = Path(raw_dir)
        audio = download_audio(
            url,
            work,
            user_agent=str(getattr(ctx, "user_agent", USER_AGENT)),
        )
        size = audio.stat().st_size
        if size > MAX_SOURCE_BYTES:
            limit_mib = MAX_SOURCE_BYTES // (1024 * 1024)
            raise ReachError(BAD_INPUT, f"audio exceeds the {limit_mib} MiB source limit")

        duration = probe_duration(ctx, audio)
        compressed = compress_audio(ctx, audio, work)
        if compressed.stat().st_size <= SIZE_LIMIT_BYTES:
            chunks = [compressed]
        else:
            chunks = chunk_audio(ctx, compressed, work)

        if len(chunks) > MAX_CHUNKS:
            raise ReachError(
                BAD_INPUT,
                f"audio produced {len(chunks)} chunks; the limit is {MAX_CHUNKS}",
            )

        chunk_sizes = []
        for chunk in chunks:
            chunk_size = chunk.stat().st_size
            if chunk_size > SIZE_LIMIT_BYTES:
                raise ReachError(
                    UPSTREAM_BROKEN,
                    f"audio chunk {chunk.name} exceeds the provider upload limit",
                )
            chunk_sizes.append(chunk_size)
        if sum(chunk_sizes) > MAX_TOTAL_CHUNK_BYTES:
            limit_mib = MAX_TOTAL_CHUNK_BYTES // (1024 * 1024)
            raise ReachError(
                BAD_INPUT, f"audio chunks exceed the {limit_mib} MiB total limit"
            )

        pieces: list[str] = []
        polish_errors: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            text = transcribe_chunk(
                chunk,
                provider=provider_name,
                key=key,
                model=model_name,
                language=language,
                prompt=_language_prompt(language),
            )
            if polish and polish_key:
                text, polish_error = polish_text(text, polish_key)
                if polish_error:
                    polish_errors.append(f"chunk {index}: {polish_error}")
            pieces.append(text.strip())

    transcript = "\n".join(piece for piece in pieces if piece)
    if not transcript:
        raise ReachError(
            UPSTREAM_BROKEN, f"{provider_name} returned an empty transcript"
        )
    return {
        "text": transcript,
        "provider": provider_name,
        "model": model_name,
        "language": language or None,
        "duration_seconds": round(duration, 1),
        "chunks": len(chunks),
        "polished": bool(polish and not polish_errors),
        "polish_error": "; ".join(polish_errors) or None,
    }
