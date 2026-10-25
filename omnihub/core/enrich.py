"""Optional ``--enrich N``: read the first few search results in place.

Search returns links plus snippets; an agent that needs the body can either
call ``read`` itself or ask for enrichment. Enrichment fetches at most *N*
results concurrently, replaces each short snippet with the read text and never
lets one failure spoil the batch: failed targets are reported in
``meta.enrich_errors`` while the original items stay intact.

Every target is re-validated with the same public-URL policy used by ``read``
before any request is made.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping

from omnihub.core.policy import ensure_public_url
from omnihub.core.result import OmniHubError, classify

MIN_TEXT_CHARS = 200
DEFAULT_CONCURRENCY = 3
DEFAULT_LIMIT = 3

ReadOne = Callable[[str], Mapping[str, Any]]


def _text_length(item: Mapping[str, Any]) -> int:
    return len(str(item.get("text") or "").strip())


def _candidates(items: list[dict], limit: int, min_chars: int) -> list[tuple[int, str]]:
    chosen: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        if len(chosen) >= limit:
            break
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        if not url or _text_length(item) >= min_chars:
            continue
        chosen.append((index, url))
    return chosen


def _read_one(url: str, read_one: ReadOne) -> dict:
    """Read one target, converting every failure into a report entry."""
    try:
        safe = ensure_public_url(url)
    except OmniHubError as exc:
        return {"url": url, "code": exc.code, "message": exc.message}
    try:
        result = read_one(safe)
    except OmniHubError as exc:
        return {"url": url, "code": exc.code, "message": exc.message}
    except Exception as exc:  # isolate every failure from the batch
        error = classify(exc)
        return {"url": url, "code": error.code, "message": error.message}
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        error = (result or {}).get("error") or {}
        return {
            "url": url,
            "code": str(error.get("code") or "UPSTREAM_BROKEN"),
            "message": str(error.get("message") or "read returned no content"),
        }
    read_items = result.get("items") or []
    if not read_items:
        return {"url": url, "code": "NOT_FOUND", "message": "read returned no content"}
    return {
        "url": url,
        "item": read_items[0],
        "platform": result.get("channel"),
        "source": result.get("backend"),
    }


def _merge(item: dict, outcome: Mapping[str, Any]) -> None:
    read_item = outcome.get("item") or {}
    text = str(read_item.get("text") or "").strip()
    if not text:
        return
    raw = dict(item["raw"]) if isinstance(item.get("raw"), dict) else {}
    if item.get("text"):
        raw.setdefault("search_text", item["text"])
    for key in ("title", "author", "published_at", "url"):
        if not item.get(key) and read_item.get(key):
            item[key] = read_item[key]
    item["text"] = text
    read_meta = read_item.get("raw") if isinstance(read_item.get("raw"), dict) else {}
    raw["enriched"] = {
        "platform": outcome.get("platform"),
        "source": outcome.get("source"),
        "chars": len(text),
        "truncated": read_meta.get("truncated"),
    }
    item["raw"] = raw


def enrich_items(
    payload: Mapping[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    read_one: ReadOne,
    concurrency: int = DEFAULT_CONCURRENCY,
    min_chars: int = MIN_TEXT_CHARS,
) -> dict:
    """Return *payload* with the first qualifying items read in place.

    Never changes ``ok`` or the top-level error: search results remain usable
    even when every read fails.
    """
    result: dict = dict(payload)
    meta = dict(result.get("meta") or {})
    result["meta"] = meta
    meta["enriched"] = 0
    meta["enrich_errors"] = []
    if result.get("ok") is not True or limit < 1:
        return result

    items = [
        dict(item) if isinstance(item, Mapping) else item
        for item in (result.get("items") or [])
    ]
    targets = _candidates(items, limit, min_chars)
    if not targets:
        meta["enrich_skipped"] = "no result needed a body"
        return result

    workers = max(1, min(int(concurrency), len(targets)))
    outcomes: dict[str, Mapping[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_read_one, url, read_one): url for _, url in targets
        }
        for future, url in futures.items():
            try:
                outcomes[url] = future.result()
            except Exception as exc:  # a worker must never lose the batch
                error = classify(exc)
                outcomes[url] = {"url": url, "code": error.code, "message": error.message}

    enriched = 0
    errors: list[dict] = []
    for index, url in targets:
        outcome = outcomes.get(url) or {
            "url": url,
            "code": "UPSTREAM_BROKEN",
            "message": "read did not complete",
        }
        if "item" in outcome:
            _merge(items[index], outcome)
            enriched += 1
        else:
            errors.append(
                {
                    "url": url,
                    "code": outcome.get("code") or "UPSTREAM_BROKEN",
                    "message": str(outcome.get("message") or "")[:300],
                }
            )

    result["items"] = items
    meta["enriched"] = enriched
    meta["enrich_errors"] = errors
    return result
