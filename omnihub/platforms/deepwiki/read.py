"""Read a public repository's DeepWiki wiki through the hosted MCP endpoint.

DeepWiki indexes public GitHub repositories and exposes three MCP tools:
``read_wiki_structure``, ``read_wiki_contents`` and ``ask_wiki_question``.
This platform maps ``read`` to the full wiki. Accepting ``owner/repo``, a
GitHub URL or a DeepWiki URL keeps explicit requests and URL routing on the
same code path.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from omnihub.core.result import (
    BAD_INPUT,
    NOT_FOUND,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    OmniHubError,
    make_item,
)

_MCP_URL = "https://mcp.deepwiki.com/mcp"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_DEEPWIKI_HOSTS = frozenset({"deepwiki.com", "www.deepwiki.com"})
_LIMIT_RE = re.compile(r"rate[ _-]?limit|too many requests|\b429\b", re.IGNORECASE)


def _repo_of(target: Any) -> str:
    text = str(target or "").strip()
    if not text:
        raise OmniHubError(BAD_INPUT, "deepwiki read requires owner/repo or a repository URL")

    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    path = text
    if "://" in text:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        if host not in _GITHUB_HOSTS | _DEEPWIKI_HOSTS:
            raise OmniHubError(
                BAD_INPUT,
                f"deepwiki cannot read {host or text!r}; use a github.com or deepwiki.com URL",
            )
        path = parts.path
    else:
        for host in sorted(_GITHUB_HOSTS | _DEEPWIKI_HOSTS, key=len, reverse=True):
            if text == host or text.startswith(host + "/"):
                path = text[len(host) :]
                break

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        raise OmniHubError(BAD_INPUT, f"deepwiki needs owner/repo, got {target!r}")
    repo = f"{segments[0]}/{segments[1]}"
    if not _REPO_RE.match(repo):
        raise OmniHubError(BAD_INPUT, f"deepwiki needs owner/repo, got {target!r}")
    return repo


def _payload(raw: Any) -> dict:
    payload = getattr(raw, "data", None)
    if isinstance(payload, dict):
        return payload
    text = str(getattr(raw, "text", "") or "")
    chunks = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    candidate = chunks[-1] if chunks else text
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        raise OmniHubError(UPSTREAM_BROKEN, "DeepWiki returned an unrecognized response") from None
    if not isinstance(value, dict):
        raise OmniHubError(UPSTREAM_BROKEN, "DeepWiki returned an unexpected payload")
    return value


def _text_of(payload: dict) -> str:
    if "error" in payload:
        error = payload.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        if code in (-32602, "-32602"):
            raise OmniHubError(BAD_INPUT, f"DeepWiki rejected the request: {message}"[:500])
        raise OmniHubError(UPSTREAM_BROKEN, f"DeepWiki: {message}"[:500])

    result = payload.get("result") or {}
    content = result.get("content") or []
    text = "\n".join(
        str(entry.get("text", ""))
        for entry in content
        if isinstance(entry, dict) and entry.get("type") == "text"
    ).strip()
    if result.get("isError"):
        if _LIMIT_RE.search(text):
            raise OmniHubError(RATE_LIMITED, text[:500])
        if "not found" in text.casefold() or "index it" in text.casefold():
            raise OmniHubError(
                NOT_FOUND,
                text[:500] or "DeepWiki has no wiki for this repository",
                "Open the repository on deepwiki.com once to index it, then retry.",
            )
        raise OmniHubError(UPSTREAM_BROKEN, text[:500] or "DeepWiki returned an error")
    if not text:
        raise OmniHubError(UPSTREAM_BROKEN, "DeepWiki returned no wiki content")
    return text


def read_wiki(target: Any, *, params: Optional[dict] = None, ctx: Any = None) -> list[dict]:
    repo = _repo_of(target)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "read_wiki_contents",
            "arguments": {"repoName": repo},
        },
    }
    raw = ctx.http(
        _MCP_URL,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        data=json.dumps(body),
    )
    text = _text_of(_payload(raw))
    return [
        make_item(
            title=f"{repo} — DeepWiki",
            url=f"https://deepwiki.com/{repo}",
            text=text,
            raw={"repo": repo, "tool": "read_wiki_contents"},
        )
    ]
