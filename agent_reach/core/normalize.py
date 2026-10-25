"""Turn parser output into the shared item shape.

Parsers may return a list, a single object, raw text or already-normalized
items. Provenance fields are filled here, never in parsers, so every source
gets consistent ``platform`` / ``source`` / ``priority`` values.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from agent_reach.core.result import make_item
from agent_reach.parse.generic import as_item, extract_items
from agent_reach.utils.url import normalize_public_http_url

_CORE_KEYS = ("title", "url", "author", "published_at", "text", "raw")
_ITEM_HINTS = frozenset({"title", "url", "link", "text", "content", "author", "published_at"})


def canonical_url(url: Optional[str]) -> Optional[str]:
    """Normalize HTTP(S) URLs for dedupe; leave non-HTTP values untouched."""
    if not url:
        return None
    candidate = str(url).strip()
    if candidate.startswith(("http://", "https://")):
        try:
            return normalize_public_http_url(candidate)
        except ValueError:
            return candidate
    return candidate


def _looks_like_item(entry: Mapping[str, Any]) -> bool:
    return any(key in entry for key in _CORE_KEYS) or bool(_ITEM_HINTS.intersection(entry))


def _coerce_item(entry: Any, *, action: str, target: Optional[str]) -> dict:
    if isinstance(entry, Mapping):
        if _looks_like_item(entry):
            base = dict(entry)
            for key in _CORE_KEYS:
                base.setdefault(key, None)
            return base
        return as_item(dict(entry))
    if isinstance(entry, str):
        if action == "read":
            return make_item(url=target, text=entry)
        if entry.startswith(("http://", "https://")):
            return make_item(url=entry)
        return make_item(text=entry)
    return make_item(text=str(entry))


def normalize_items(
    value: Any,
    *,
    platform: str,
    source: str,
    priority: int,
    action: str,
    target: Optional[str] = None,
) -> list[dict]:
    """Normalize a parser value and attach provenance to every item."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        if _looks_like_item(value):
            entries: Iterable[Any] = [value]
        else:
            try:
                entries = extract_items(value)
            except ValueError:
                entries = [value]
    elif isinstance(value, (list, tuple)):
        entries = value
    elif isinstance(value, str):
        entries = [value]
    else:
        entries = [value]

    items: list[dict] = []
    for entry in entries:
        item = _coerce_item(entry, action=action, target=target)
        item["url"] = canonical_url(item.get("url"))
        if item.get("platform") is None:
            item["platform"] = platform
        if item.get("source") is None:
            item["source"] = source
        if item.get("priority") is None:
            item["priority"] = priority
        items.append(item)
    return items


def _dedupe_key(item: Mapping[str, Any]) -> tuple:
    url = canonical_url(item.get("url"))
    if url:
        return ("url", url.split("#", 1)[0])
    title = (item.get("title") or "").strip().lower()
    text = (item.get("text") or "").strip().lower()[:200]
    return ("text", title, text)


def dedupe_items(items: Iterable[dict]) -> list[dict]:
    """Keep the first item for each URL (fragment-insensitive) or title/text pair."""
    seen: set[tuple] = set()
    unique: list[dict] = []
    for item in items:
        key = _dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
