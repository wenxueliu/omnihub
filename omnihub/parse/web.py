"""Shared Exa MCP and Jina Reader parsers.

Exa's hosted MCP replies with JSON-RPC, often as Server-Sent Events. Jina
returns a small header block followed by ``Markdown Content:``. Both are
normalized to plain item dicts here; the pipeline attaches provenance. Any
platform may name these handlers in ``parse:`` (``exa_search``, ``exa_read``,
``jina``); a platform-local ``parse.py`` with the same name still wins.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from omnihub.core.result import (
    BAD_INPUT,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    OmniHubError,
    make_item,
)

_EXA_HEADER_RE = re.compile(r"^(Title|URL|Published|Author):\s*(.*)$")
_EXA_LIMIT_RE = re.compile(
    r"\b429\b|rate[ _-]?limit|too many requests|\bquota\b|"
    r"(?:usage|request|daily|monthly|free)[ _-]+limit|"
    r"(?:credit|credits|balance).{0,40}(?:exhaust|deplet|insufficient|exceed|reach)|"
    r"(?:out of|insufficient|exhausted).{0,20}credits?",
    re.IGNORECASE,
)


def _exa_error(text: str, rpc_code: Any = None) -> OmniHubError:
    if rpc_code in (-32602, "-32602"):
        return OmniHubError(BAD_INPUT, f"Exa rejected the arguments: {text[:300]}")
    if rpc_code in (429, "429") or _EXA_LIMIT_RE.search(text or ""):
        return OmniHubError(
            RATE_LIMITED,
            f"Exa: {text[:300]}",
            "Exa's hosted search limit was reached. Use the agent's built-in web "
            "search or wait before retrying.",
        )
    return OmniHubError(UPSTREAM_BROKEN, f"Exa: {text[:300]}")


def _is_antibot_page(text: str) -> bool:
    sample = (text or "")[:4096].casefold()
    jina_captcha_warning = "warning:" in sample and "requiring captcha" in sample
    challenge_structure = any(
        marker in sample
        for marker in (
            "title: just a moment...",
            "## performing security verification",
            "title: attention required! | cloudflare",
        )
    )
    cloudflare_block = "title: attention required! | cloudflare" in sample and (
        "ray id" in sample or "/cdn-cgi/challenge-platform/" in sample
    )
    return (jina_captcha_warning and challenge_structure) or cloudflare_block


def _exa_payload_text(raw: Any) -> str:
    if isinstance(getattr(raw, "data", None), dict):
        payload = raw.data
    else:
        text = str(getattr(raw, "text", "") or "")
        chunks = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        candidate = chunks[-1] if chunks else text
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            raise OmniHubError(
                UPSTREAM_BROKEN, "Exa returned an unrecognized response"
            ) from None
    if not isinstance(payload, dict):
        raise OmniHubError(UPSTREAM_BROKEN, "Exa returned an unexpected payload")
    if "error" in payload:
        error = payload.get("error") or {}
        if isinstance(error, dict):
            raise _exa_error(str(error.get("message", error)), error.get("code"))
        raise _exa_error(str(error))
    result = payload.get("result") or {}
    content = result.get("content") or []
    text = "\n".join(
        str(entry.get("text", ""))
        for entry in content
        if isinstance(entry, dict) and entry.get("type") == "text"
    )
    if result.get("isError"):
        raise _exa_error(text)
    return text


def _parse_exa_text(text: str) -> list[dict]:
    items: list[dict] = []
    for block in re.split(r"\n(?=Title: )", "\n" + (text or "").strip()):
        block = block.strip()
        if not block.startswith("Title:"):
            continue
        fields: dict[str, str] = {}
        body: list[str] = []
        lines = block.splitlines()
        index = 0
        while index < len(lines):
            match = _EXA_HEADER_RE.match(lines[index])
            if not match:
                break
            fields[match.group(1).lower()] = match.group(2).strip()
            index += 1
        for line in lines[index:]:
            if line.strip() in ("Highlights:", "Text:"):
                continue
            body.append(line)

        def clean(value: Optional[str]) -> Optional[str]:
            return None if value in (None, "", "N/A") else value

        items.append(
            make_item(
                title=clean(fields.get("title")),
                url=clean(fields.get("url")),
                author=clean(fields.get("author")),
                published_at=clean(fields.get("published")),
                text="\n".join(body).strip(),
                raw=fields,
            )
        )
    return items


def _parse_exa_page(text: str) -> dict:
    """Parse a single fetched page (Exa emits a Markdown heading, not ``Title:``)."""
    lines = (text or "").strip().splitlines()
    index = 0
    title: Optional[str] = None
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip() or None
        index = 1
    fields: dict[str, str] = {}
    while index < len(lines):
        match = _EXA_HEADER_RE.match(lines[index])
        if not match:
            break
        fields[match.group(1).lower()] = match.group(2).strip()
        index += 1
    body = "\n".join(lines[index:]).strip()
    return make_item(
        title=title or fields.get("title") or None,
        url=fields.get("url") or None,
        author=fields.get("author") or None,
        published_at=fields.get("published") or None,
        text=body,
        raw=fields,
    )


def _parse_jina(target: str, body: str) -> dict:
    title = re.search(r"^Title:\s*(.*)$", body, re.M)
    published = re.search(r"^Published Time:\s*(.*)$", body, re.M)
    content = body.split("Markdown Content:", 1)[1] if "Markdown Content:" in body else body
    return make_item(
        title=title.group(1).strip() if title else None,
        url=target,
        published_at=published.group(1).strip() if published else None,
        text=content.strip(),
    )


def _target(raw: Any) -> str:
    params = getattr(raw, "meta", {}).get("params") or {}
    target = params.get("target")
    if not target:
        raise OmniHubError(BAD_INPUT, "web read requires a target URL")
    return str(target)


def exa_search(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    text = _exa_payload_text(raw)
    items = _parse_exa_text(text)
    if not items:
        # Never report an unrecognized response as a successful zero-hit search.
        raise OmniHubError(UPSTREAM_BROKEN, "Exa returned no recognizable search results")
    params = getattr(raw, "meta", {}).get("params") or {}
    try:
        limit = int(params.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    return items[: max(0, limit)]


def exa_read(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    target = _target(raw)
    text = _exa_payload_text(raw)
    items = _parse_exa_text(text)
    if items:
        items[0]["url"] = items[0].get("url") or target
        return items[:1]
    if not text.strip():
        raise OmniHubError(UPSTREAM_BROKEN, "Exa returned no content")
    item = _parse_exa_page(text)
    item["url"] = item.get("url") or target
    return [item]


def jina(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    target = _target(raw)
    body = str(getattr(raw, "text", "") or "")
    if _is_antibot_page(body):
        raise OmniHubError(
            UPSTREAM_BROKEN,
            "Jina Reader returned an anti-bot verification page instead of the target content",
        )
    if not body.strip():
        raise OmniHubError(UPSTREAM_BROKEN, "Jina Reader returned no content")
    return [_parse_jina(target, body)]
