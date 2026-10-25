---
name: github
description: Search GitHub repositories and read a repository, issue, or pull request. Use when the user asks to find open-source projects, inspect a GitHub repo, or read an issue/PR. 中文触发词：GitHub、仓库搜索、开源项目、issue、pull request、PR 内容。
---

# github

Searches GitHub repositories through the public search API and reads a repository, issue, or pull request as structured text. The platform is declarative except for its request parser and read script.

## When to use

- The user wants open-source projects: 「找几个 Python 的 RSS 项目」「GitHub 上搜一下 xxx」.
- The user shares a GitHub URL or `owner/repo` and wants a summary: 「看看这个仓库」「读一下这个 issue / PR」.
- Not suitable for private repositories you cannot access without a token.

## Commands

| Goal | Command |
|---|---|
| Search repositories | `omnihub search "<query>" --platform github --limit 5 --text` |
| Read a repository | `omnihub read pallets/flask --platform github --text` |
| Read an issue / PR | `omnihub read https://github.com/owner/repo/issues/42 --platform github --text` |
| Health check | `omnihub doctor --text` |

GitHub search qualifiers (`language:python`, `stars:>1000`, `topic:cli`) are passed through unchanged.

## Parameters

### `search`
- `query` — `str`, required. Repository search query.
- `limit` — `int`, default `5`, range `1–100`.

### `read`
- `target` — `str`, required. A GitHub URL or `owner/repo`, optionally with `/issues/<n>` or `/pull/<n>`.

## Multi-source use

- GitHub is priority `100`, so it is preferred for repository intent and for `github.com` URLs.
- Omit `--platform` to let routing choose; use `--mode aggregate` to combine GitHub with the public web when the user wants a broad survey.
- `read <github-url>` is routed here automatically by the `github.com` / `api.github.com` / `raw.githubusercontent.com` domain match.
- Pass `--platform github` when the user explicitly names GitHub.

## Requirements

- Adapters: `http` and `builtin`.
- Authentication is optional. When `GH_TOKEN` or `GITHUB_TOKEN` is present in the environment, the token raises the rate limit; without it the unauthenticated endpoint is used.
- No login or browser is needed.

## Source order

1. `github-gh-token` — `http`, priority `10`, used only when `GH_TOKEN` is set.
2. `github-token` — `http`, priority `11`, used only when `GITHUB_TOKEN` is set.
3. `github-api` — `http`, priority `20`, unauthenticated fallback.
4. `github-api` — `builtin` read, call `read`, script `scripts/read.py`, priority `10`.
5. `scripts/parse.py` — repository search parser declared as `parse_script`.

## Output

One JSON envelope per call (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`). `--text` prints the human-readable form. Repository items include `title`/full name, `url`, `description`, stars, language, and timestamps; read items carry the repository or thread body.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | A required adapter or builtin script is unavailable; reinstall the package. |
| `RATE_LIMITED` | The anonymous quota is exhausted; set `GITHUB_TOKEN` or retry later. |
| `NOT_FOUND` | The repository, issue, or pull request does not exist or is private. |
| `UPSTREAM_BROKEN` | GitHub returned an unexpected payload shape. |
| `BAD_INPUT` | The target is not a GitHub URL or `owner/repo`. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/parse.py` — repository search response parser.
- `scripts/read.py` — repository / issue / pull-request reader.
