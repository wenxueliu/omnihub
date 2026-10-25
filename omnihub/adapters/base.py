"""Contracts shared by every tool adapter.

Platform manifests never spawn processes or touch the network directly. They
name an adapter (``http``, ``cli``, ``opencli``, ``daemon``, ``mcp``, ...) plus
the call to make; the adapter owns transport, timeouts, output limits and
error mapping. Keeping that boundary narrow is what makes a platform
deletable without touching shared code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

#: Adapter kinds a manifest may reference. ``builtin`` is for platforms that
#: ship their own Python implementation and need no external tool.
ADAPTER_KINDS = frozenset(
    {"builtin", "http", "cli", "opencli", "daemon", "mcp", "browser"}
)


class AdapterError(Exception):
    """A transport/tool failure carrying a unified OmniHub error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True)
class Health:
    """Result of probing a tool or daemon before an action."""

    ok: bool
    version: Optional[str] = None
    detail: str = ""
    logged_in: Optional[bool] = None


@dataclass(frozen=True)
class ToolCall:
    """One resolved call from a platform manifest source."""

    adapter: str
    call: Optional[str] = None
    action: Optional[str] = None
    tool: Optional[str] = None
    #: Absolute path to a script bundled by the platform. The adapter reads
    #: it; script text is never templated or sent as a resolved argument.
    script: Optional[str] = None
    args: Mapping[str, Any] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    argv: Optional[tuple[str, ...]] = None
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Optional[Mapping[str, Any]] = None
    data: Optional[Any] = None
    output: str = "json"
    url: Optional[str] = None
    site: Optional[str] = None
    session: Optional[str] = None
    parse: tuple[str, ...] = ()
    timeout: Optional[float] = None
    max_bytes: Optional[int] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RawResult:
    """Unparsed adapter output. ``data`` is set for structured transports."""

    ok: bool = True
    data: Any = None
    text: Optional[str] = None
    content_type: str = "application/json"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int
    truncated: bool = False


@runtime_checkable
class Context(Protocol):
    """Everything an adapter is allowed to use for one action."""

    timeout: float

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        input_data: Optional[str] = None,
        cwd: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> ProcessResult:
        ...

    def http(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        data: Any = None,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> RawResult:
        ...

    def secret(self, name: str) -> Optional[str]:
        ...


@runtime_checkable
class Adapter(Protocol):
    """Transport implementation. Adapters are stateless between actions."""

    kind: str

    def probe(self) -> Health:
        ...

    def invoke(self, call: ToolCall, *, ctx: Context) -> RawResult:
        ...
