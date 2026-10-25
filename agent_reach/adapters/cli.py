"""Generic CLI adapter.

The manifest either gives an explicit ``argv`` (recommended, unambiguous) or
names an executable/tool plus a call. In the latter case mapping is simple:
``{"query": "x", "limit": 5}`` becomes ``--query x --limit 5``; booleans become
a bare flag. No shell is ever involved.
"""

from __future__ import annotations

from typing import Any, Sequence

from agent_reach.adapters.base import Health, RawResult, ToolCall
from agent_reach.core.result import BAD_INPUT, ReachError


def _flag(key: str) -> str:
    return "--" + key.strip().lstrip("-").replace("_", "-")


def args_to_argv(args: dict[str, Any]) -> list[str]:
    argv: list[str] = []
    for key, value in args.items():
        if value is None or value is False:
            continue
        flag = _flag(str(key))
        if value is True:
            argv.append(flag)
        elif isinstance(value, (list, tuple)):
            for entry in value:
                argv.extend([flag, str(entry)])
        else:
            argv.extend([flag, str(value)])
    return argv


def build_argv(call: ToolCall) -> tuple[str, ...]:
    if call.argv:
        return tuple(str(part) for part in call.argv)
    executable = call.tool or call.call
    if not executable:
        raise ReachError(BAD_INPUT, "cli source requires argv or tool/call")
    argv: list[str] = [executable]
    if call.tool and call.call:
        argv.append(call.call)
    argv.extend(args_to_argv(dict(call.args) or dict(call.params)))
    return tuple(argv)


class CliAdapter:
    kind = "cli"

    def probe(self) -> Health:
        return Health(ok=True)

    def invoke(self, call: ToolCall, *, ctx: Any) -> RawResult:
        argv: Sequence[str] = build_argv(call)
        process = ctx.run(
            argv,
            timeout=call.timeout,
            max_bytes=call.max_bytes,
        )
        return RawResult(
            ok=True,
            data=None,
            text=process.stdout,
            content_type="text/plain",
            meta={
                "argv": list(argv),
                "returncode": process.returncode,
                "truncated": process.truncated,
                "stderr": process.stderr[-500:] if process.stderr else "",
            },
        )
