"""Parsers for GitHub REST API responses.

Search goes through ``/search/repositories``; the returned repository objects
are reduced to the fields an agent actually needs so the envelope does not
carry the full API payload.
"""

from __future__ import annotations

import json
from typing import Any

from omnihub.core.result import UPSTREAM_BROKEN, OmniHubError, make_item


def _payload(raw: Any) -> dict:
    data = getattr(raw, "data", None)
    if isinstance(data, dict):
        return data
    text = str(getattr(raw, "text", "") or "")
    try:
        value = json.loads(text)
    except ValueError:
        raise OmniHubError(UPSTREAM_BROKEN, "GitHub returned invalid JSON") from None
    if not isinstance(value, dict):
        raise OmniHubError(UPSTREAM_BROKEN, "GitHub returned an unexpected payload")
    return value


def _limit(raw: Any, default: int = 5) -> int:
    params = getattr(raw, "meta", {}).get("params") or {}
    try:
        return max(0, int(params.get("limit", default)))
    except (TypeError, ValueError):
        return default


def _login(owner: Any) -> Any:
    return owner.get("login") if isinstance(owner, dict) else None


def github_repos(raw: Any, *, action: Any = None, ctx: Any = None) -> list[dict]:
    payload = _payload(raw)
    repos = payload.get("items")
    if not isinstance(repos, list):
        message = str(payload.get("message") or "no repository list").strip()
        raise OmniHubError(UPSTREAM_BROKEN, f"GitHub search failed: {message}")

    items: list[dict] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        items.append(
            make_item(
                title=repo.get("full_name") or repo.get("name"),
                url=repo.get("html_url"),
                author=_login(repo.get("owner")),
                published_at=repo.get("created_at"),
                text=repo.get("description"),
                raw={
                    "full_name": repo.get("full_name"),
                    "description": repo.get("description"),
                    "language": repo.get("language"),
                    "stars": repo.get("stargazers_count"),
                    "forks": repo.get("forks_count"),
                    "open_issues": repo.get("open_issues_count"),
                    "topics": repo.get("topics"),
                    "archived": repo.get("archived"),
                    "updated_at": repo.get("updated_at"),
                },
            )
        )
    return items[: _limit(raw)]
