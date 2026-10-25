---
name: twitter
description: Search X/Twitter posts and read a thread through the logged-in browser. Use when the user asks what X/Twitter says, wants recent tweets on a topic, or shares an x.com/twitter.com status link. 中文触发词：推特、Twitter、X、推文搜索、热搜、看看推特怎么说。
---

# twitter

Searches X/Twitter posts through the user's logged-in browser (with an Exa fallback) and reads a tweet thread in the same session. The browser scripts are bundled under `scripts/`.

## When to use

- The user wants real-time discussion: 「推特上怎么说」「搜一下 X 上的讨论」.
- The user shares an `x.com` or `twitter.com` status URL and wants the text: 「读一下这条推文」.
- Not suitable when the browser is logged out — X gates most search results behind a session.

## Commands

| Goal | Command |
|---|---|
| Search posts | `omnihub search "<query>" --platform twitter --limit 5 --text` |
| Read a tweet | `omnihub read <status-url> --platform twitter --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. A tweet URL on `x.com`, `twitter.com`, or `mobile.twitter.com`.

## Multi-source use

- Twitter is priority `150`; `x.com` / `twitter.com` URLs route here automatically.
- `--mode aggregate` combines it with Reddit, V2EX, or the public web for cross-platform sentiment questions.
- Omit `--platform` for routing; pass it when the user names Twitter/X.

## Requirements

- Adapters: `http` (Exa fallback) and `daemon` (browser).
- Browser: a logged-in X session. Login is done by the user; the skill never automates it.
- No API secret.

## Source order

1. `kimi-browser` search — `daemon`, priority `10`, call `browse`, script `scripts/search.js`, `when: adapter:daemon`.
2. `exa` search — `http`, priority `20`, site-restricted fallback.
3. `kimi-browser` read — `daemon`, priority `10`, call `browse`, script `scripts/read.js`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include tweet text, URL, author, and timestamp; read items carry the article text.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon is not running; start it and retry. |
| `NEED_LOGIN` | X redirected to its login flow; ask the user to sign in. |
| `PLATFORM_BLOCKED` | Rate limit or error page; back off, do not loop. |
| `NOT_FOUND` | The tweet is deleted or the page has no article. |
| `TIMEOUT` | The timeline did not load; retry once. |
| `BAD_INPUT` | The target is not an X/Twitter URL. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.js` — browser script that extracts timeline articles.
- `scripts/read.js` — browser script that extracts one tweet.
