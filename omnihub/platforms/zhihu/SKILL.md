---
name: zhihu
description: Search Zhihu questions and read a question, answer, or zhuanlan article through the logged-in browser. Use when the user asks what Zhihu says, wants discussions or answers on a topic, or shares a zhihu.com / zhuanlan.zhihu.com link. 中文触发词：知乎、知乎搜索、知乎回答、知乎文章、看看知乎怎么说。
---

# zhihu

Searches Zhihu content and reads a question, answer, or zhuanlan article through the user's logged-in browser. The browser scripts are bundled under `scripts/`.

## When to use

- The user wants Chinese-language discussion or long-form answers: 「知乎上怎么说」「搜一下知乎的讨论」.
- The user shares a `zhihu.com/question/...`, `zhihu.com/question/.../answer/...`, or `zhuanlan.zhihu.com/p/...` URL and wants the text: 「读一下这篇知乎」.
- Not suitable when the browser is logged out — Zhihu gates search and full answers behind a session.

## Commands

| Goal | Command |
|---|---|
| Search content | `omnihub search "<query>" --platform zhihu --limit 5 --text` |
| Read a question / answer / article | `omnihub read <zhihu-url> --platform zhihu --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. A `zhihu.com/question/<id>`, `zhihu.com/question/<id>/answer/<id>`, or `zhuanlan.zhihu.com/p/<id>` URL.

## Multi-source use

- Zhihu is priority `230`; `zhihu.com` / `zhuanlan.zhihu.com` URLs route here automatically.
- `--mode aggregate` combines it with the public web or other social platforms for cross-platform questions.
- Omit `--platform` for routing; pass it when the user names Zhihu.

## Requirements

- Adapters: `daemon` (browser) and `http` (optional Jina Reader).
- Browser: a logged-in Zhihu session. Login is done by the user; the skill never automates it.
- Credential: `JINA_API_KEY` is optional. When set, `read` tries `https://r.jina.ai` first and falls back to the browser if Jina fails.

## Source order

1. `kimi-browser` search — `daemon`, priority `10`, call `browse`, script `scripts/search.js`, `when: adapter:daemon`.
2. `jina` read — `http`, priority `5`, `https://r.jina.ai/<target>`, `when: secret:JINA_API_KEY`.
3. `kimi-browser` read — `daemon`, priority `10`, call `browse`, script `scripts/read.js`; default when no key is set and fallback when Jina fails.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title, URL, author, relative publish time, and excerpt. Read items carry the question/answer/article text; a question URL returns up to the first three answers, an answer URL returns that answer, and an article URL returns the article body.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon is not running; start it and retry. |
| `NEED_LOGIN` | Zhihu redirected to its sign-in flow; ask the user to sign in. |
| `PLATFORM_BLOCKED` | Rate limit or captcha page; back off, do not loop. |
| `NOT_FOUND` | The question/answer/article is deleted or the page has no content. |
| `TIMEOUT` | The page did not render in time; retry once. |
| `BAD_INPUT` | The target is not a Zhihu URL. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.js` — browser script that extracts search-result cards.
- `scripts/read.js` — browser script that extracts the question header and answers, or an article body.
