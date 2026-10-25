---
name: web
description: General public-web search and page reading through Exa and Jina Reader, with a browser fallback. Use for any research question that no specialized platform covers. 中文触发词：网页搜索、通用搜索、上网查、读网页、打开链接、公网。
---

# web

The public-web fallback. It searches the open web with Exa Hosted MCP and reads pages with Jina Reader, the logged-in browser, or Exa. It is fully declarative: no bundled scripts.

## When to use

- Any general research request: 「上网查一下 xxx」「帮我搜一下最新消息」.
- Reading an arbitrary public URL that no specialized platform owns: 「读一下这个网页」.
- Not the best choice when a specialized platform exists (GitHub, Reddit, V2EX, Bilibili, WeChat, etc.); prefer those for their domains.

## Commands

| Goal | Command |
|---|---|
| Search the web | `omnihub search "<query>" --platform web --limit 5 --text` |
| Read a page | `omnihub read <url> --platform web --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. What to search for.
- `limit` — `int`, default `5`.

### `read`
- `target` — `str`, required, URL. A public page URL.

## Multi-source use

- `priority: 10` and `public_web: true`: routing places it last, after every matching specialized platform.
- Omit `--platform` to let routing pick; for a broad question, `--mode aggregate` runs the web alongside specialized platforms.
- `search --enrich N` reads the first `N` results that only have snippets.
- Pass `--platform web` to force general search, for example when specialized results are thin.

## Requirements

- Adapters: `http` (Exa, Jina) and optionally `daemon` (browser read).
- No secret is required for the hosted Exa endpoint or Jina Reader.
- Browser fallback is used only when the daemon is available.

## Source order

1. `exa` search — `http`, priority `10`.
2. `jina` read — `http`, priority `10`.
3. `kimi-browser` read — `daemon`, priority `20`, `when: adapter:daemon`.
4. `exa` read — `http`, priority `30`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title, URL, and snippet; read items carry the page text.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | No HTTP adapter is available; check the OmniHub install. |
| `PLATFORM_BLOCKED` | The site refused Jina/Exa and/or the browser; try another source. |
| `NOT_FOUND` | The URL does not exist or returns an empty page. |
| `TIMEOUT` | The page or search backend was slow; retry once. |
| `UPSTREAM_BROKEN` | Exa or Jina returned an unexpected shape. |
| `BAD_INPUT` | The target is not an absolute URL. |

## Bundled files

- `platform.yaml` — actions, parameters, and sources. This platform ships no scripts.
