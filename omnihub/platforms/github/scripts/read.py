"""Read a repository, issue or pull request through GitHub's REST API.

The target may be a full URL (``https://github.com/owner/repo``), a short
``owner/repo`` reference, or an issue/PR URL. Anything else returns no items so
the pipeline can fall back to the generic web reader. Authentication is
optional: ``GITHUB_TOKEN`` or ``GH_TOKEN`` raises the rate limit and unlocks
private repositories, and is never stored in the result.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import quote

from omnihub.core.result import NOT_FOUND, UPSTREAM_BROKEN, OmniHubError, make_item

_API = "https://api.github.com"
_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/?#]+)/(?P<repo>[^/?#]+)"
    r"(?P<rest>/[^?#]*)?",
    re.IGNORECASE,
)
_SHORT_RE = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)"
    r"(?P<rest>/(?:issues|pull)/\d+)?/?$",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"^/(?:issues|pull)/(?P<number>\d+)/?$", re.IGNORECASE)


def _parse_target(target: Any) -> Optional[tuple[str, str, Optional[int]]]:
    value = str(target or "").strip()
    match = _URL_RE.match(value)
    if match:
        owner = match.group("owner")
        repo = match.group("repo")
        rest = match.group("rest") or ""
    else:
        match = _SHORT_RE.match(value)
        if not match:
            return None
        owner = match.group("owner")
        repo = match.group("repo")
        rest = match.group("rest") or ""
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    number_match = _NUMBER_RE.match(rest)
    number = int(number_match.group("number")) if number_match else None
    return owner, repo, number


def _headers(ctx: Any) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = None
    if ctx is not None and hasattr(ctx, "secret"):
        token = ctx.secret("GH_TOKEN") or ctx.secret("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json(ctx: Any, url: str, headers: dict[str, str], *, accept: Optional[str] = None) -> Any:
    request_headers = dict(headers)
    if accept:
        request_headers["Accept"] = accept
    raw = ctx.http(url, headers=request_headers)
    data = getattr(raw, "data", None)
    if isinstance(data, (dict, list)):
        return data
    text = str(getattr(raw, "text", "") or "")
    try:
        return json.loads(text)
    except ValueError:
        raise OmniHubError(UPSTREAM_BROKEN, "GitHub returned invalid JSON") from None


def _repo_item(ctx: Any, owner: str, repo: str, headers: dict[str, str]) -> dict:
    path = f"{quote(owner, safe='')}/{quote(repo, safe='')}"
    data = _json(ctx, f"{_API}/repos/{path}", headers)
    if not isinstance(data, dict):
        raise OmniHubError(UPSTREAM_BROKEN, "GitHub returned an unexpected repository payload")

    readme: Optional[str] = None
    try:
        raw = ctx.http(
            f"{_API}/repos/{path}/readme",
            headers={**headers, "Accept": "application/vnd.github.raw"},
        )
        readme = str(getattr(raw, "text", "") or "").strip() or None
    except OmniHubError as exc:
        if exc.code != NOT_FOUND:
            raise

    description = data.get("description")
    text = "\n\n".join(part for part in (description, readme) if part)
    return make_item(
        title=data.get("full_name") or f"{owner}/{repo}",
        url=data.get("html_url") or f"https://github.com/{owner}/{repo}",
        author=(data.get("owner") or {}).get("login")
        if isinstance(data.get("owner"), dict)
        else None,
        published_at=data.get("created_at"),
        text=text or None,
        raw={
            "full_name": data.get("full_name"),
            "description": description,
            "language": data.get("language"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "topics": data.get("topics"),
            "archived": data.get("archived"),
            "default_branch": data.get("default_branch"),
            "updated_at": data.get("updated_at"),
            "readme_chars": len(readme or ""),
        },
    )


def _comments_text(comments: Any) -> tuple[list[str], str]:
    lines: list[str] = []
    if not isinstance(comments, list):
        return lines, ""
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "").strip()
        if not body:
            continue
        author = (comment.get("user") or {}).get("login") or "unknown"
        lines.append(f"**@{author}** ({comment.get('created_at') or ''}):\n{body}")
    if not lines:
        return [], ""
    return lines, "\n\n---\n\n## Comments\n\n" + "\n\n".join(lines)


def _issue_item(ctx: Any, owner: str, repo: str, number: int, headers: dict[str, str]) -> dict:
    path = f"{quote(owner, safe='')}/{quote(repo, safe='')}"
    url = f"{_API}/repos/{path}/issues/{number}"
    data = _json(ctx, url, headers)
    if not isinstance(data, dict):
        raise OmniHubError(UPSTREAM_BROKEN, "GitHub returned an unexpected issue payload")

    comments: Any = []
    try:
        comments = _json(ctx, f"{url}/comments?per_page=50", headers)
    except OmniHubError as exc:
        # Comments are supplementary; only a hard failure should abort the read.
        if exc.code != NOT_FOUND:
            raise
    lines, comments_text = _comments_text(comments)
    body = str(data.get("body") or "").strip()
    is_pull = "pull_request" in data
    return make_item(
        title=data.get("title") or f"{owner}/{repo}#{number}",
        url=data.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}",
        author=(data.get("user") or {}).get("login")
        if isinstance(data.get("user"), dict)
        else None,
        published_at=data.get("created_at"),
        text=(body + comments_text).strip() or None,
        raw={
            "kind": "pull_request" if is_pull else "issue",
            "number": data.get("number") or number,
            "state": data.get("state"),
            "labels": [
                label.get("name")
                for label in (data.get("labels") or [])
                if isinstance(label, dict) and label.get("name")
            ],
            "comments": len(lines),
            "updated_at": data.get("updated_at"),
        },
    )


def read(target: Any, *, params: Any = None, ctx: Any = None) -> list[dict]:
    parsed = _parse_target(target)
    if parsed is None:
        # Not a repository reference: let another platform or the web reader try.
        return []
    owner, repo, number = parsed
    headers = _headers(ctx)
    if number is not None:
        return [_issue_item(ctx, owner, repo, number, headers)]
    return [_repo_item(ctx, owner, repo, headers)]
