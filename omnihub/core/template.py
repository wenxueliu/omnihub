"""Resolve ``${...}`` placeholders in manifests.

Only two forms are supported:

* ``${name}`` reads a validated action parameter.
* ``${secret:NAME}`` reads a credential through ``ctx.secret``.

A string that is exactly one placeholder keeps the parameter's native type, so
``numResults: "${limit}"`` becomes an integer in a JSON body. A placeholder
embedded in a larger string is stringified.

Filters after ``|`` transform a value: ``${query|urlencode}``,
``${name|lower}``, ``${name|upper}`` and ``${name|strip}``. Filters may be
combined, and always produce a string.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote_plus

from omnihub.core.result import BAD_INPUT, NEED_SETUP, OmniHubError

_PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")
_FULL = re.compile(r"^\$\{([^{}]+)\}$")


def _apply_filter(value: Any, name: str) -> str:
    text = str(value)
    if name == "urlencode":
        return quote_plus(text, safe="")
    if name == "lower":
        return text.lower()
    if name == "upper":
        return text.upper()
    if name == "strip":
        return text.strip()
    raise OmniHubError(
        NEED_SETUP,
        f"unknown template filter {name!r} (use urlencode, lower, upper or strip)",
    )


def _lookup(token: str, params: Mapping[str, Any], ctx: Any) -> Any:
    parts = [part.strip() for part in token.split("|")]
    source = parts[0]
    filters = [part for part in parts[1:] if part]
    if source.startswith("secret:"):
        name = source.split(":", 1)[1].strip()
        if not name:
            raise OmniHubError(BAD_INPUT, "empty secret name in manifest template")
        value = ctx.secret(name)
        if value is None:
            raise OmniHubError(
                NEED_SETUP,
                f"missing secret {name!r}; set it in the environment or ~/.omnihub/secrets.yaml",
            )
    elif source in params:
        value = params[source]
    else:
        raise OmniHubError(
            NEED_SETUP, f"manifest references undeclared parameter {source!r}"
        )
    for name in filters:
        value = _apply_filter(value, name)
    return value


def resolve_templates(value: Any, params: Mapping[str, Any], ctx: Any) -> Any:
    """Recursively substitute placeholders in a manifest value."""
    if isinstance(value, str):
        full = _FULL.match(value)
        if full:
            token = full.group(1)
            if "|" not in token:
                return _lookup(token, params, ctx)
            return str(_lookup(token, params, ctx))
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
        raise OmniHubError(BAD_INPUT, "manifest argv must be a list")
    return tuple(str(part) for part in resolved)
