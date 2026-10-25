"""Search the user's NotebookLM notebooks by title.

The CLI has no server-side title filter, so this source lists notebooks with
``notebooklm list --json`` and filters locally. Every item carries the notebook
id in ``text`` so a follow-up ``read`` can address it directly.
"""

from __future__ import annotations

from typing import Any, Optional

from omnihub.core.result import UPSTREAM_BROKEN, OmniHubError, make_item
from omnihub.parse.generic import ParseError, loads_json

_ITEM_KEYS = ("notebooks", "items", "results", "data", "list")


def _list_of(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _ITEM_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _notebooks(raw: Any) -> list[dict]:
    try:
        payload = loads_json(str(getattr(raw, "stdout", "") or ""))
    except ParseError:
        raise OmniHubError(
            UPSTREAM_BROKEN, "notebooklm list returned invalid JSON"
        ) from None
    entries = _list_of(payload)
    if not entries and not isinstance(payload, (dict, list)):
        raise OmniHubError(UPSTREAM_BROKEN, "notebooklm list returned an unexpected payload")
    return [entry for entry in entries if isinstance(entry, dict)]


def _matches(entry: dict, query: str) -> bool:
    if not query:
        return True
    needle = query.casefold()
    return any(
        needle in str(entry.get(key) or "").casefold()
        for key in ("title", "id")
    )


def _item(entry: dict) -> dict:
    notebook_id = str(entry.get("id") or "")
    title = str(entry.get("title") or notebook_id or "Untitled notebook")
    created = entry.get("created_at")
    modified = entry.get("modified_at")
    detail = [f"notebook id: {notebook_id or '(unknown)'}"]
    if created:
        detail.append(f"created: {created}")
    if modified:
        detail.append(f"modified: {modified}")
    if entry.get("is_owner") is not None:
        detail.append("access: " + ("owner" if entry.get("is_owner") else "shared"))
    return make_item(
        title=title,
        url=(
            f"https://notebooklm.google.com/notebook/{notebook_id}"
            if notebook_id
            else None
        ),
        published_at=created,
        text="\n".join(detail),
        raw=entry,
    )


def search_notebooks(
    query: Any, *, limit: int = 5, params: Optional[dict] = None, ctx: Any = None
) -> list[dict]:
    raw = ctx.run(["notebooklm", "list", "--json"])
    matches = [entry for entry in _notebooks(raw) if _matches(entry, str(query or ""))]
    try:
        count = max(0, int(limit))
    except (TypeError, ValueError):
        count = 5
    return [_item(entry) for entry in matches[:count]]
