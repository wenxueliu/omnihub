"""Shared safety policy: timeouts, output caps and URL validation.

Platform manifests may request smaller limits, but they cannot raise the
ceilings set here. Keeping this in core means a third-party platform cannot
weaken SSRF or resource protections by editing its own YAML.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agent_reach.core.result import BAD_INPUT, FINAL_CODES, ReachError, classify
from agent_reach.utils.url import host_matches, normalize_public_http_url

DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 300.0

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Limits:
    timeout: float = DEFAULT_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES

    @classmethod
    def resolve(
        cls,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> "Limits":
        return cls(
            timeout=clamp_timeout(timeout),
            max_bytes=clamp_max_bytes(max_bytes),
        )


def clamp_timeout(value: Optional[float], *, default: float = DEFAULT_TIMEOUT) -> float:
    """Validate a caller timeout and cap it at the hard ceiling."""
    if value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ReachError(BAD_INPUT, "timeout must be a number") from None
    if seconds < MIN_TIMEOUT:
        raise ReachError(BAD_INPUT, f"timeout must be at least {MIN_TIMEOUT:g}s")
    return min(seconds, MAX_TIMEOUT)


def clamp_max_bytes(value: Optional[int], *, default: int = DEFAULT_MAX_BYTES) -> int:
    """Validate an output cap and cap it at the hard ceiling."""
    if value is None:
        return default
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise ReachError(BAD_INPUT, "max_bytes must be an integer") from None
    if size < 1:
        raise ReachError(BAD_INPUT, "max_bytes must be positive")
    return min(size, MAX_MAX_BYTES)


def ensure_public_url(url: str) -> str:
    """Normalize a URL, rejecting loopback/private/lookalike targets."""
    try:
        return normalize_public_http_url(url)
    except ValueError as exc:
        raise ReachError(BAD_INPUT, str(exc)) from None


def url_matches(url: str, *domains: str) -> bool:
    """Exact-host or real-subdomain match; userinfo and non-HTTP are rejected."""
    return host_matches(url, *domains)


def is_final(error: BaseException) -> bool:
    """Whether fallback to another source cannot help."""
    return classify(error).code in FINAL_CODES
