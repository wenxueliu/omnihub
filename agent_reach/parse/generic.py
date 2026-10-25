"""Ordered parser chain for noisy upstream output.

A platform source declares handlers in priority order, for example::

    parse: [json, json_items]

The first handler that produces a value wins. JSON handlers are deliberately
tolerant: CLIs sometimes print log lines around the payload, or wrap it in a
Markdown fence. When nothing parses, the next handler is tried instead of
failing the whole action.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Sequence

import yaml

from agent_reach.core.result import make_item
from agent_reach.parse.daemon import daemon_page
from agent_reach.parse.web import exa_read, exa_search, jina

#: Parsers shared by several platforms. A platform-local ``parse.py`` handler
#: with the same name takes precedence.
NAMED_PARSERS = {
    "exa_search": exa_search,
    "exa_read": exa_read,
    "jina": jina,
    "daemon_page": daemon_page,
}

_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_START = re.compile(r"[\[{]")
_ITEM_KEYS = ("items", "results", "search_results", "hits", "documents", "data", "list")


class ParseError(ValueError):
    """Raised by one handler; the chain moves on to the next handler."""


def loads_json(text: str) -> Any:
    """Parse JSON from clean or noisy stdout."""
    candidate = str(text or "").strip()
    if not candidate:
        raise ParseError("empty output")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    fenced = _FENCED_JSON.search(candidate)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    # Salvage the first JSON value after leading log lines.
    match = _JSON_START.search(candidate)
    if match:
        try:
            value, _ = json.JSONDecoder().raw_decode(candidate[match.start() :])
            return value
        except json.JSONDecodeError:
            pass
    raise ParseError("no JSON object or array found")


def _text_of(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple)):
        parts = []
        for entry in value:
            if isinstance(entry, dict):
                parts.append(str(entry.get("text") or entry.get("content") or ""))
            else:
                parts.append(str(entry))
        joined = "\n".join(part for part in parts if part)
        return joined or None
    if isinstance(value, dict):
        return _text_of(value.get("text") or value.get("content"))
    return str(value)


def _first(entry: dict, keys: Sequence[str]) -> Any:
    for key in keys:
        value = entry.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def as_item(entry: Any) -> dict:
    if isinstance(entry, str):
        if entry.startswith(("http://", "https://")):
            return make_item(url=entry)
        return make_item(text=entry)
    if not isinstance(entry, dict):
        return make_item(text=str(entry))

    return make_item(
        title=_text_of(_first(entry, ("title", "name", "heading"))),
        url=_text_of(_first(entry, ("url", "link", "href", "target"))),
        author=_text_of(
            _first(entry, ("author", "username", "user", "owner", "site_name", "channel"))
        ),
        published_at=_text_of(
            _first(
                entry,
                ("published_at", "published", "publish_date", "date", "created_at", "timestamp"),
            )
        ),
        text=_text_of(
            _first(entry, ("text", "content", "description", "snippet", "body", "summary"))
        ),
        raw=entry,
    )


def extract_items(payload: Any) -> list[dict]:
    """Find the item list inside a common search-response shape."""
    if isinstance(payload, str):
        payload = loads_json(payload)
    if isinstance(payload, list):
        return [as_item(entry) for entry in payload]
    if not isinstance(payload, dict):
        raise ParseError(f"cannot extract items from {type(payload).__name__}")

    for key in _ITEM_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [as_item(entry) for entry in value]
        if isinstance(value, dict):
            try:
                return extract_items(value)
            except ParseError:
                continue
    raise ParseError("no item list found in response")


def _select(payload: Any, selector: str) -> Any:
    """Resolve a dotted selector such as ``data.items`` or ``data.0.text``."""
    current = payload
    for part in (piece for piece in selector.split(".") if piece):
        if isinstance(current, dict):
            if part not in current:
                raise ParseError(f"selector {selector!r} not found")
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                raise ParseError(f"selector {selector!r} not found") from None
        else:
            raise ParseError(f"selector {selector!r} not found")
    return current


def _payload_of(raw: Any) -> Any:
    return getattr(raw, "data", raw)


def _text_of_raw(raw: Any) -> str:
    text = getattr(raw, "text", None)
    payload = _payload_of(raw)
    if text is not None:
        return str(text)
    if isinstance(payload, str):
        return payload
    return ""


def _parse_json(raw: Any, selector: str) -> Any:
    payload = _payload_of(raw)
    if isinstance(payload, (dict, list)):
        value = payload
    else:
        value = loads_json(_text_of_raw(raw))
    return _select(value, selector) if selector else value


def parse_with_chain(
    raw: Any,
    chain: Sequence[str],
    *,
    custom: Optional[dict[str, Any]] = None,
    action: Optional[str] = None,
    ctx: Any = None,
) -> Any:
    """Run handlers in order and return the first successful value.

    Handler names: ``json``, ``json_items``, ``yaml``, ``yaml_items``,
    ``opencli``, ``text``, ``markdown`` and ``raw``. Append ``:<dotted.path>``
    to a JSON/YAML handler to select a sub-value, e.g.
    ``json_items:data.results``. A platform may supply extra named handlers in
    its ``parse.py``; ``custom`` maps those names to callables.
    """
    if not chain:
        payload = _payload_of(raw)
        return payload if payload is not None else _text_of_raw(raw)

    custom = custom or {}
    errors: list[str] = []
    for entry in chain:
        name, _, selector = str(entry).partition(":")
        name = name.strip().lower()
        if name in custom:
            try:
                return custom[name](raw, action=action, ctx=ctx)
            except ParseError as exc:
                errors.append(f"{entry}: {exc}")
                continue
        if name in NAMED_PARSERS:
            try:
                return NAMED_PARSERS[name](raw, action=action, ctx=ctx)
            except ParseError as exc:
                errors.append(f"{entry}: {exc}")
                continue
        try:
            if name == "json":
                return _parse_json(raw, selector)
            if name == "json_items":
                return extract_items(_parse_json(raw, selector))
            if name == "yaml":
                payload = _payload_of(raw)
                value = payload if isinstance(payload, (dict, list)) else yaml.safe_load(_text_of_raw(raw))
                if not isinstance(value, (dict, list)):
                    raise ParseError("YAML payload is not structured")
                return _select(value, selector) if selector else value
            if name == "yaml_items":
                payload = _payload_of(raw)
                value = payload if isinstance(payload, (dict, list)) else yaml.safe_load(_text_of_raw(raw))
                if not isinstance(value, (dict, list)):
                    raise ParseError("YAML payload is not structured")
                return extract_items(_select(value, selector) if selector else value)
            if name in {"opencli", "yaml-envelope"}:
                payload = _payload_of(raw)
                value = payload if isinstance(payload, (dict, list)) else yaml.safe_load(_text_of_raw(raw))
                if not isinstance(value, dict):
                    raise ParseError("no OpenCLI envelope")
                if value.get("ok") is False:
                    error = value.get("error") or {}
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    raise ParseError(f"OpenCLI error: {message or 'unknown error'}")
                if selector in ("", "generic", "items"):
                    return extract_items(value)
                return _select(value, selector)
            if name in {"text", "markdown"}:
                text = _text_of_raw(raw)
                if not selector and not text:
                    raise ParseError("empty text")
                return _select({"text": text}, selector) if selector else text
            if name in {"html", "regex"}:
                raise ParseError("HTML/regex parsers are not available yet")
            if name == "raw":
                return _payload_of(raw)
            raise ParseError(f"unknown parser {name!r}")
        except (ParseError, yaml.YAMLError, ValueError, TypeError) as exc:
            errors.append(f"{entry}: {exc}")

    raise ParseError("; ".join(errors) or "no parser configured")
