---
name: reddit
description: Search Reddit posts and read public threads. Use when the user asks what Reddit says about a topic, wants subreddit discussions, or shares a reddit.com/redd.it link. 中文触发词：Reddit、红迪、子版块、帖子搜索、看看 Reddit 怎么说。
---

# reddit

Searches Reddit posts through the logged-in browser's JSON endpoint (with an Exa fallback) and reads public threads through the browser or Exa. Fully declarative apart from the browser search script.

## When to use

- The user wants English-language community opinion: 「Reddit 上怎么说」「搜一下 r/Python」.
- The user shares a Reddit thread and wants its content: 「读一下这个帖子」.
- Not suitable for private subreddits or content hidden behind an account you do not have.

## Commands

| Goal | Command |
|---|---|
| Search posts | `omnihub search "<query>" --platform reddit --limit 5 --text` |
| Read a thread | `omnihub read <reddit-url> --platform reddit --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. A thread URL on `reddit.com` / `old.reddit.com`, or a `redd.it` short link.

## Multi-source use

- Reddit is priority `130`; explicit Reddit URLs route here before the public-web fallback.
- `--mode aggregate` pairs it well with V2EX, Hacker News-style web results, or Twitter for cross-community questions.
- Omit `--platform` for routing; pass it whenever the user names Reddit.

## Requirements

- Adapters: `http` (Exa) and `daemon` (browser search/read).
- Search through the browser uses the user's Reddit session and cookies. If logged out, Reddit may still serve public results; a verification page yields `PLATFORM_BLOCKED`.
- No API secret.

## Source order

1. `kimi-browser` search — `daemon`, priority `10`, call `browse`, script `scripts/search.js`, `when: adapter:daemon`.
2. `exa` search — `http`, priority `20`, site-restricted fallback.
3. `kimi-browser` read — `daemon`, priority `10`, built-in `read` action.
4. `exa` read — `http`, priority `20`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title, URL, author (`u/...`), subreddit, score, comment count, and a text preview; read items carry the thread body.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon is not running; start it or use the Exa fallback. |
| `NEED_LOGIN` | Reddit requires a session for this content; ask the user to sign in. |
| `PLATFORM_BLOCKED` | Rate-limit or verification page; back off, do not loop. |
| `NOT_FOUND` | The thread was deleted or the URL is wrong. |
| `TIMEOUT` | The browser page did not settle; retry once. |
| `BAD_INPUT` | The target is not a Reddit URL. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.js` — browser script that calls `/search.json` with the page query.
