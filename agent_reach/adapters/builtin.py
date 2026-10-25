"""Built-in Python hooks shipped inside a platform directory.

Declarative manifests should cover most platforms. When a platform genuinely
needs code, ``search.py`` / ``read.py`` may export a function named by the
source's ``call``. Third-party platforms are deliberately not executed: only
``origin == "builtin"`` is trusted until an explicit trust flag exists.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any

from agent_reach.adapters.base import Health, RawResult, ToolCall
from agent_reach.core.result import NEED_SETUP, ReachError, classify

_LOCK = threading.Lock()
_MODULES: dict[tuple[str, int, int], Any] = {}


def _load_module(path: Path, platform_id: str) -> Any:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        cached = _MODULES.get(key)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(
            f"reach_platform_{platform_id}_{path.stem}_{abs(hash(key))}", path
        )
        if spec is None or spec.loader is None:
            raise ReachError(NEED_SETUP, f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[key] = module
        return module


class BuiltinAdapter:
    kind = "builtin"

    def probe(self) -> Health:
        return Health(ok=True)

    def invoke(self, call: ToolCall, *, ctx: Any) -> RawResult:
        platform_path = call.raw.get("platform_path")
        platform_id = call.raw.get("platform_id") or "platform"
        origin = call.raw.get("platform_origin")
        if origin != "builtin":
            raise ReachError(
                NEED_SETUP,
                "third-party platform Python hooks require an explicit trust flag (not available yet)",
            )
        if not platform_path:
            raise ReachError(NEED_SETUP, "platform path missing from builtin call")
        if not call.call:
            raise ReachError(NEED_SETUP, "builtin source requires a call")

        module_path = Path(platform_path) / f"{call.action or 'action'}.py"
        if not module_path.is_file():
            raise ReachError(NEED_SETUP, f"platform has no {module_path.name}")
        try:
            module = _load_module(module_path, str(platform_id))
            function = getattr(module, call.call)
        except ReachError:
            raise
        except (OSError, AttributeError, ImportError) as exc:
            raise ReachError(NEED_SETUP, f"cannot load {module_path.name}: {exc}") from None

        params = dict(call.params)
        params.update(call.args)
        try:
            if call.action == "search":
                result = function(
                    params.get("query"),
                    limit=params.get("limit", 5),
                    params=params,
                    ctx=ctx,
                )
            elif call.action == "read":
                result = function(
                    params.get("target"),
                    params=params,
                    ctx=ctx,
                )
            else:
                result = function(params=params, ctx=ctx)
        except ReachError:
            raise
        except Exception as exc:
            raise classify(exc) from None

        return _wrap(result)


def _wrap(result: Any) -> RawResult:
    if isinstance(result, RawResult):
        return result
    if isinstance(result, dict):
        if any(key in result for key in ("items", "results", "search_results", "hits", "data")):
            return RawResult(data=result)
        return RawResult(data=[result])
    if isinstance(result, (list, tuple)):
        return RawResult(data=list(result))
    if isinstance(result, str):
        return RawResult(text=result, content_type="text/plain")
    return RawResult(data=result)
