"""Adapter lookup used by the pipeline."""

from __future__ import annotations

from typing import Any

from omnihub.adapters.base import Adapter
from omnihub.adapters.builtin import BuiltinAdapter
from omnihub.adapters.cli import CliAdapter
from omnihub.adapters.daemon import DaemonAdapter
from omnihub.adapters.http import HttpAdapter
from omnihub.adapters.opencli import OpenCliAdapter


def build_adapters(*, context: Any = None) -> dict[str, Adapter]:
    """Return the adapters implemented in this milestone.

    ``mcp`` and ``browser`` are reserved kinds. A manifest that names one
    falls through with NEED_SETUP until its adapter lands. The daemon adapter
    keeps the action context so its health probe uses the same bounded
    loopback transport as its calls.
    """
    return {
        "builtin": BuiltinAdapter(),
        "http": HttpAdapter(),
        "cli": CliAdapter(),
        "opencli": OpenCliAdapter(),
        "daemon": DaemonAdapter(context=context),
    }
