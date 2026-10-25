"""Resolve ``${...}`` placeholders in manifests.

Only two forms are supported:

* ``${name}`` reads a validated action parameter.
* ``${secret:NAME}`` reads a credential through ``ctx.secret``.

A string that is exactly one placeholder keeps the parameter's native type, so
``numResults: "${limit}"`` becomes an integer in a JSON body. A placeholder
embedded in a larger string is stringified.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from agent_reach.core.result import BAD_INPUT, NEED_SETUP, ReachError

_PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")
_FULL = re.compile(r"^\$\{([^{}]+)\}$")


def _lookup(token: str, params: Mapping[str, Any], ctx: Any) -> Any:
    token = token.strip()
    if token.startswith("secret:"):
        name = token.split(":", 1)[1].strip()
        if not name:
            raise ReachError(BAD_INPUT, "empty secret name in manifest template")
        value = ctx.secret(name)
        if value is None:
            raise ReachError(
                NEED_SETUP,
                f"missing secret {name!r}; set it in the environment or ~/.agent-reach/secrets.yaml",
            )
        return value
    if token not in params:
        raise ReachError(
            NEED_SETUP, f"manifest references undeclared parameter {token!r}"
        )
    return params[token]


def resolve_templates(value: Any, params: Mapping[str, Any], ctx: Any) -> Any:
    """Recursively substitute placeholders in a manifest value."""
    if isinstance(value, str):
        full = _FULL.match(value)
        if full:
            return _lookup(full.group(1), params, ctx)
        return _PLACEHOLDER.sub(lambda match: str(_lookup(match.group(1), params, ctx)), value)
    if isinstance(value, dict):
        return {
            resolve_templates(key, params, ctx) if isinstance(key, str) else key:
            resolve_templates(item, params, ctx)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [resolve_templates(item, params, ctx) for item in value]
    return value


def resolve_argv(value: Any, params: Mapping[str, Any], ctx: Any) -> tuple[str, ...] | None:
    """Resolve an argv template and stringify every element (no shell involved)."""
    if value is None:
        return None
    resolved = resolve_templates(value, params, ctx)
    if not isinstance(resolved, (list, tuple)):
        raise ReachError(BAD_INPUT, "manifest argv must be a list")
    return tuple(str(part) for part in resolved)
