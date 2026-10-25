"""Shared tool adapters used by platform manifests."""

from omnihub.adapters.base import (
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
