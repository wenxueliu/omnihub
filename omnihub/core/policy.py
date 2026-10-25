"""Shared safety policy: timeouts, output caps and URL validation.

Platform manifests may request smaller limits, but they cannot raise the
ceilings set here. Keeping this in core means a third-party platform cannot
weaken SSRF or resource protections by editing its own YAML.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from omnihub.core.result import (
    BAD_INPUT,
    FINAL_CODES,
    UPSTREAM_BROKEN,
    OmniHubError,
    classify,
)
from omnihub.utils.url import host_matches, normalize_public_http_url

DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 300.0

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_MAX_BYTES = 64 * 1024 * 1024

MAX_MIN_INTERVAL = 10.0


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
        raise OmniHubError(BAD_INPUT, "timeout must be a number") from None
    if seconds < MIN_TIMEOUT:
        raise OmniHubError(BAD_INPUT, f"timeout must be at least {MIN_TIMEOUT:g}s")
    return min(seconds, MAX_TIMEOUT)


def clamp_max_bytes(value: Optional[int], *, default: int = DEFAULT_MAX_BYTES) -> int:
    """Validate an output cap and cap it at the hard ceiling."""
    if value is None:
        return default
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise OmniHubError(BAD_INPUT, "max_bytes must be an integer") from None
    if size < 1:
        raise OmniHubError(BAD_INPUT, "max_bytes must be positive")
    return min(size, MAX_MAX_BYTES)


def clamp_min_interval(value: Optional[float]) -> float:
    """Validate a source pacing interval and cap how long a caller may sleep."""
    if value is None:
        return 0.0
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise OmniHubError(BAD_INPUT, "min_interval must be a number") from None
    if seconds < 0:
        raise OmniHubError(BAD_INPUT, "min_interval cannot be negative")
    return min(seconds, MAX_MIN_INTERVAL)


def ensure_public_url(url: str) -> str:
    """Normalize a URL, rejecting loopback/private/lookalike targets."""
    try:
        return normalize_public_http_url(url)
    except ValueError as exc:
        raise OmniHubError(BAD_INPUT, str(exc)) from None


def ensure_public_resolution(url: str) -> list[str]:
    """Resolve the URL's host and reject non-public answers.

    ``ensure_public_url`` only inspects the literal host, so a public-looking
    name that resolves to ``127.0.0.1`` (``localtest.me`` is a common example)
    would otherwise reach local services. Every answer must be globally
    routable. Requests resolves the name again when it connects, so a DNS
    rebinding attacker still has a narrow window; this closes the ordinary
    case without pinning the connection.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        raise OmniHubError(BAD_INPUT, "only public HTTP(S) URLs are allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    try:
        ascii_host = host.encode("ascii").decode("ascii")
    except UnicodeError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise OmniHubError(BAD_INPUT, f"cannot resolve {host!r}") from None
    try:
        infos = socket.getaddrinfo(ascii_host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise OmniHubError(UPSTREAM_BROKEN, f"cannot resolve {host!r}: {exc}") from None
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise OmniHubError(BAD_INPUT, f"cannot resolve {host!r}") from None
        if not address.is_global:
            raise OmniHubError(
                BAD_INPUT,
                f"refusing to fetch {host!r}: it resolves to the "
                f"non-public address {address}",
            )
        addresses.append(str(address))
    if not addresses:
        raise OmniHubError(UPSTREAM_BROKEN, f"cannot resolve {host!r}")
    return addresses


def url_matches(url: str, *domains: str) -> bool:
    """Exact-host or real-subdomain match; userinfo and non-HTTP are rejected."""
    return host_matches(url, *domains)


def is_final(error: BaseException) -> bool:
    """Whether fallback to another source cannot help."""
    return classify(error).code in FINAL_CODES
