---
name: notebooklm
description: Find Google NotebookLM notebooks and read a notebook's summary through the local notebooklm CLI. Use when the user asks about their NotebookLM notebooks, wants a notebook summary, or shares a notebooklm.google.com link. 中文触发词：NotebookLM、笔记本、知识库、笔记本摘要、谷歌笔记。
---

# notebooklm

Finds NotebookLM notebooks and reads a notebook summary by shelling out to the local `notebooklm` CLI. Both actions are bundled builtin scripts; the platform requires an authenticated CLI session.

## When to use

- The user wants to locate their own notebooks: 「我的 NotebookLM 里有哪些笔记」「找一下关于 xxx 的笔记本」.
- The user shares a notebook id or `notebooklm.google.com` URL and wants a summary: 「读一下这个笔记本」.
- Not suitable when the `notebooklm` CLI is not installed or not authenticated; use the web platform instead.

## Commands

| Goal | Command |
|---|---|
| List / search notebooks | `omnihub search "" --platform notebooklm --limit 10 --text` |
| Filter by title | `omnihub search "<text>" --platform notebooklm --text` |
| Read a notebook | `omnihub read <notebook-id> --platform notebooklm --text` |
| Read from a URL | `omnihub read https://notebooklm.google.com/notebook/<id> --platform notebooklm --text` |
| Health check | `omnihub doctor --text` |

An empty query lists every notebook.

## Parameters

### `search`
- `query` — `str`, required. Text matched against notebook titles; empty returns all.
- `limit` — `int`, default `5`, range `1–100`.

### `read`
- `target` — `str`, required. A notebook id (full or unique partial) or a `notebooklm.google.com` notebook URL.

## Multi-source use

- NotebookLM is priority `210` and only usable when the `notebooklm` tool is installed; the source is guarded by `when: tool:notebooklm`.
- It has no public-web fallback: OmniHub skips this platform when the tool or session is missing.
- `--mode aggregate` can combine notebook summaries with web sources for research notes.
- Pass `--platform notebooklm` when the user names NotebookLM or their notebooks.

## Requirements

- Adapter: `builtin`.
- Tool: the `notebooklm` CLI on `PATH`.
- Auth: a signed-in `notebooklm` CLI session (`auth.session: notebooklm`).
- No API secret.

## Source order

1. `notebooklm-cli` search — `builtin`, priority `10`, tool `notebooklm`, call `search_notebooks`, script `scripts/search.py`.
2. `notebooklm-cli` read — `builtin`, priority `10`, tool `notebooklm`, call `read_notebook`, script `scripts/read.py`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include notebook id, title, and metadata; read items carry the generated summary.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The `notebooklm` CLI is not installed or is on an unexpected path. |
| `NEED_LOGIN` | The CLI session expired; ask the user to sign in with the tool, then retry. |
| `NOT_FOUND` | The notebook id is unknown or the partial id matched nothing/several notebooks. |
| `TIMEOUT` | NotebookLM took too long to answer; retry later. |
| `UPSTREAM_BROKEN` | The CLI output shape changed. |
| `BAD_INPUT` | The target is not a notebook id or URL. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.py` — notebook listing and title matching.
- `scripts/read.py` — notebook summary reader.
