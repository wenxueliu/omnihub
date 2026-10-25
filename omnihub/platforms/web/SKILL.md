---
name: web
description: General public-web search through Exa, Bing or Google, plus page reading through Jina Reader with a browser fallback. Use for any research question that no specialized platform covers. 中文触发词：网页搜索、通用搜索、上网查、读网页、打开链接、公网。
---

# web

The public-web fallback. It searches with Exa Hosted MCP, Bing or Google, and
reads pages with Jina Reader, the logged-in browser, or Exa. Search sources are
selected with the `engine` parameter.

## When to use

- Any general research request: 「上网查一下 xxx」「帮我搜一下最新消息」.
- Reading an arbitrary public URL that no specialized platform owns: 「读一下这个网页」.
- Not the best choice when a specialized platform exists (GitHub, Reddit, V2EX, Bilibili, WeChat, etc.); prefer those for their domains.

## Commands

| Goal | Command |
|---|---|
| Search the web (Exa first, then Bing, then Google) | `omnihub search "<query>" --platform web --limit 5 --text` |
| Search with Bing | `omnihub search "<query>" --platform web --engine bing --limit 5 --text` |
| Search with Google (browser) | `omnihub search "<query>" --platform web --engine google --limit 5 --text` |
| Read a page | `omnihub read <url> --platform web --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. What to search for.
- `limit` — `int`, default `5`.
- `engine` — `str`, default `auto`. One of `auto`, `exa`, `bing`, `google`.
  `auto` tries Exa, then Bing, then Google until one returns results; a named
  engine runs only that backend.

### `read`
- `target` — `str`, required, URL. A public page URL.

## Multi-source use

- `priority: 10` and `public_web: true`: routing places it last, after every matching specialized platform.
- Omit `--platform` to let routing pick; for a broad question, `--mode aggregate` runs the web alongside specialized platforms.
- `search --enrich N` reads the first `N` results that only have snippets.
- Pass `--platform web` to force general search, for example when specialized results are thin.

## Requirements

- Adapters: `http` (Exa, Jina, Bing) and optionally `daemon` (Google and browser read).
- No secret is required for Exa, Bing through Jina Reader, or Jina Reader itself.
- Google search uses the logged-in browser: the `google` source is skipped when
  the kimi-webbridge daemon is not available. Bing needs no key and no browser.

## Source order

Search (`engine: auto`):
1. `exa` — `http`, priority `10`.
2. `bing` — `http` through Jina Reader on `bing.com`, priority `20`.
3. `google` — `daemon` browser, priority `30`, requires `adapter:daemon`.

Read:
1. `jina` read — `http`, priority `10`.
2. `kimi-browser` read — `daemon`, priority `20`, `when: adapter:daemon`.
3. `exa` read — `http`, priority `30`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title, URL, and snippet; read items carry the page text.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | No HTTP adapter is available, or Google was requested without the browser daemon. |
| `PLATFORM_BLOCKED` | The site refused Jina/Exa/Bing/Google and/or the browser; try another engine. |
| `NOT_FOUND` | The URL does not exist or returns an empty page. |
| `TIMEOUT` | The page or search backend was slow; retry once. |
| `UPSTREAM_BROKEN` | Exa, Bing or Jina returned an unexpected shape. |
| `BAD_INPUT` | The target is not an absolute URL. |

## Bundled files

- `platform.yaml` — actions, parameters, and sources.
- `scripts/search_google.js` — extracts result cards from a Google results page
  inside the logged-in browser.
