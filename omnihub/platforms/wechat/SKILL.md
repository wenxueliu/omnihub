---
name: wechat
description: Search WeChat Official Account articles and read an article. Use when the user asks to search 微信公众号 articles, find a public-account post, or read a mp.weixin.qq.com link. 中文触发词：微信公众号、公众号文章、搜一搜、微信文章、mp.weixin.qq.com。
---

# wechat

Searches WeChat Official Account articles through Exa (primary) with Sogou's WeChat index as a browser fallback, and reads articles in the logged-in browser using a mobile WeChat user agent. The browser scripts are bundled under `scripts/`.

## When to use

- The user wants public-account articles: 「搜一下微信公众号关于 AI 的文章」「微信上有没有讲过 xxx」.
- The user shares an `mp.weixin.qq.com` or Sogou WeChat redirect link and wants the body: 「读一下这篇公众号」.
- Not suitable for private articles, deleted posts, or content that requires the WeChat mobile client.

## Commands

| Goal | Command |
|---|---|
| Search articles | `omnihub search "<query>" --platform wechat --limit 5 --text` |
| Read an article | `omnihub read <article-url> --platform wechat --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. An `mp.weixin.qq.com` article URL, or a Sogou WeChat result redirect.

## Multi-source use

- WeChat is priority `220`; `mp.weixin.qq.com` and `weixin.sogou.com` URLs route here before the public web.
- Exa does not need a browser, so `search` works even when the daemon is down.
- `--mode aggregate` combines WeChat articles with web and Xiaohongshu results for Chinese-content research.
- Omit `--platform` for routing; pass it whenever the user names 公众号/微信.

## Requirements

- Adapters: `http` (Exa) and `daemon` (browser fallback and reads).
- Browser: for Sogou search and all reads. The read source overrides the user agent to a mobile WeChat client; login is not required for public articles, but verification pages can still appear.
- No API secret.

## Source order

1. `exa` search — `http`, priority `5`, site-restricted to `mp.weixin.qq.com`.
2. `kimi-browser` search — `daemon`, priority `20`, call `browse`, script `scripts/search.js`, `when: adapter:daemon`.
3. `kimi-browser` read — `daemon`, priority `10`, call `browse`, script `scripts/read.js`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title, URL, account, and summary; read items include title, account, author, publish time, body text, and image URLs.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon is not running; Exa search still works, reads need it. |
| `NEED_LOGIN` | Rare for public articles; ask the user to sign in if it occurs. |
| `PLATFORM_BLOCKED` | Sogou verification or WeChat 「环境异常」; resolve manually, do not loop. |
| `NOT_FOUND` | The article was deleted, migrated, or blocked by the publisher. |
| `TIMEOUT` | Sogou's page regularly exceeds the navigation cap; retry or use Exa. |
| `BAD_INPUT` | The target is not a WeChat article URL. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.js` — Sogou WeChat index result extractor.
- `scripts/read.js` — WeChat article body and metadata extractor.
