"""Read a NotebookLM notebook as an AI-generated summary.

``notebooklm summary --json`` is the closest stable read surface: it accepts a
full or partial notebook id, works without a selected notebook, and does not
mutate the notebook. A notebook URL is reduced to its id first.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from agent_reach.core.result import BAD_INPUT, UPSTREAM_BROKEN, ReachError, make_item
from agent_reach.parse.generic import ParseError, loads_json

_NOTEBOOK_URL_RE = re.compile(r"notebooklm\.google\.com/notebook/([A-Za-z0-9_-]+)")


def _notebook_id(target: Any) -> str:
    text = str(target or "").strip()
    match = _NOTEBOOK_URL_RE.search(text)
    if match:
        return match.group(1)
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not text:
        raise ReachError(BAD_INPUT, "notebooklm read requires a notebook id or URL")
    if text.startswith("-"):
        raise ReachError(
            BAD_INPUT, f"notebooklm read needs a notebook id, got {target!r}"
        )
    return text


def read_notebook(target: Any, *, params: Optional[dict] = None, ctx: Any = None) -> list[dict]:
    requested = _notebook_id(target)
    raw = ctx.run(
        ["notebooklm", "summary", "--json", "--topics", "--notebook", requested]
    )
    try:
        payload = loads_json(str(getattr(raw, "stdout", "") or ""))
    except ParseError:
        raise ReachError(
            UPSTREAM_BROKEN, "notebooklm summary returned invalid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise ReachError(UPSTREAM_BROKEN, "notebooklm summary returned an unexpected payload")

    notebook_id = str(payload.get("notebook_id") or requested)
    summary = str(payload.get("summary") or "").strip()
    topics = [
        str(topic)
        for topic in payload.get("suggested_topics") or ()
        if str(topic).strip()
    ]
    text = summary or "No summary is available for this notebook yet."
    if topics:
        text += "\n\nSuggested topics:\n" + "\n".join(
            f"- {topic}" for topic in topics
        )
    return [
        make_item(
            title=f"NotebookLM notebook {notebook_id}",
            url=f"https://notebooklm.google.com/notebook/{notebook_id}",
            text=text,
            raw=payload,
        )
    ]
