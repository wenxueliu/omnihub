---
name: xueqiu
description: Search Xueqiu stock discussions and read a status through the logged-in browser. Use when the user asks what Xueqiu investors say about a stock or wants a 雪球 post read. 中文触发词：雪球、雪球搜索、股票讨论、个股情绪、看看雪球怎么说。
---

# xueqiu

Searches Xueqiu discussions and reads an individual status in the user's logged-in browser. Both behaviours are implemented as bundled browser scripts.

## When to use

- The user wants Chinese investor sentiment: 「雪球上怎么看茅台」「搜一下雪球的讨论」.
- The user shares a `xueqiu.com/<uid>/<status>` URL and wants its content: 「读一下这条雪球」.
- Not suitable when the browser is logged out — Xueqiu gates search and many pages behind a session.

## Commands

| Goal | Command |
|---|---|
| Search discussions | `omnihub search "<query>" --platform xueqiu --limit 5 --text` |
| Read a status | `omnihub read <status-url> --platform xueqiu --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Keywords, typically a ticker or company name.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. A Xueqiu status URL.

## Multi-source use

- Xueqiu is priority `160`; `xueqiu.com` URLs route here automatically.
- `--mode aggregate` combines it with the public web for 「新闻 + 社区情绪」 style questions.
- Omit `--platform` for routing; pass it when the user names 雪球.

## Requirements

- Adapter: `daemon` only.
- Browser: a logged-in Xueqiu session. Login is done by the user.
- No API secret.

## Source order

1. `kimi-browser` search — `daemon`, priority `10`, call `browse`, script `scripts/search.js`, `when: adapter:daemon`.
2. `kimi-browser` read — `daemon`, priority `10`, call `browse`, script `scripts/read.js`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title/content excerpt, URL, author, and date; read items carry the status text.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon is not running; start it and retry. |
| `NEED_LOGIN` | Xueqiu redirected to login or shows 「登录后」; ask the user to sign in. |
| `PLATFORM_BLOCKED` | Verification / 「访问过于频繁」 page; resolve manually, do not loop. |
| `NOT_FOUND` | The status page has no content or was removed. |
| `TIMEOUT` | The timeline did not load; retry once. |
| `BAD_INPUT` | The target is not a Xueqiu URL. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.js` — browser script that extracts timeline items.
- `scripts/read.js` — browser script that extracts a status page.
