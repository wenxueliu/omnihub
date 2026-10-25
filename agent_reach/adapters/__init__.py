"""Shared tool adapters used by platform manifests."""

from agent_reach.adapters.base import (
    ADAPTER_KINDS,
    Adapter,
    AdapterError,
    Context,
    Health,
    RawResult,
    ToolCall,
)

__all__ = [
    "ADAPTER_KINDS",
    "Adapter",
    "AdapterError",
    "Context",
    "Health",
    "RawResult",
    "ToolCall",
]
