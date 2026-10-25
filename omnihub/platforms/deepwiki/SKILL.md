---
name: deepwiki
description: Read DeepWiki's generated documentation for a GitHub repository. Use when the user asks for a repository architecture overview, wants DeepWiki's wiki, or shares a deepwiki.com link. 中文触发词：DeepWiki、仓库文档、项目架构、代码维基、仓库解读。
---

# deepwiki

Reads DeepWiki's generated documentation for a repository through its public MCP endpoint. The platform exposes a single `read` action; the request logic is in `scripts/read.py`.

## When to use

- The user wants a repository overview without cloning it: 「用 DeepWiki 看一下这个项目的架构」「这个仓库是怎么组织的」.
- The user shares `owner/repo`, a `github.com` URL, or a `deepwiki.com` URL and wants the wiki content.
- Not suitable for repositories that DeepWiki has not indexed, or for exact source code (use `github` or raw files for that).

## Commands

| Goal | Command |
|---|---|
| Read a repository wiki | `omnihub read facebook/react --platform deepwiki --text` |
| Read from a GitHub URL | `omnihub read https://github.com/facebook/react --platform deepwiki --text` |
| Read from a DeepWiki URL | `omnihub read https://deepwiki.com/facebook/react --platform deepwiki --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `read`
- `target` — `str`, required. `owner/repo`, a `github.com` repository URL, or a `deepwiki.com` URL.

## Multi-source use

- DeepWiki is priority `200` and has no search action, so it is only selected for `read`; `deepwiki.com` URLs route here automatically.
- `--mode aggregate` on a `read` can combine DeepWiki with `github` and `web` for a richer repository report.
- Pass `--platform deepwiki` when the user names DeepWiki.

## Requirements

- Adapter: `builtin`.
- No secret, login, or browser. The public MCP endpoint must be reachable.

## Source order

1. `deepwiki-mcp` — `builtin`, priority `10`, call `read_wiki`, script `scripts/read.py`, parse `json_items`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. The item carries the wiki text and the resolved repository identifier.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The builtin script is unavailable; reinstall the package. |
| `NOT_FOUND` | DeepWiki has not indexed this repository. |
| `TIMEOUT` | The wiki is still generating or the endpoint is slow; retry later. |
| `UPSTREAM_BROKEN` | The MCP response shape changed. |
| `BAD_INPUT` | The target is not `owner/repo` or a supported URL. |

## Bundled files

- `platform.yaml` — action, parameters, source, script.
- `scripts/read.py` — DeepWiki MCP client and response normalizer.
