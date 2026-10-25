---
name: xiaohongshu
description: Search Xiaohongshu (小红书) notes and read a note through the logged-in browser. Use when the user asks to search 小红书, find notes on a topic, or read a note/body/author. 中文触发词：小红书、小红书搜索、笔记、种草、正文、xhs、xiaohongshu。
---

# xiaohongshu

Searches Xiaohongshu notes and reads a note in the user's real, logged-in browser. It is read-only: it never likes, comments, follows, or posts. The platform bundles both browser scripts and a standalone CLI that adds token caching; see `references/xiaohongshu-cli.md`.

## When to use

- The user wants note discovery: 「小红书搜一下咖啡」「看看小红书上的攻略」.
- The user shares a note URL and wants its body: 「读一下这篇小红书」「这篇笔记写了什么」.
- Not suitable without a logged-in browser session — Xiaohongshu serves login walls and verification pages to anonymous traffic.

## Commands

| Goal | Command |
|---|---|
| Search notes | `omnihub search "<query>" --platform xiaohongshu --limit 5 --text` |
| Read a note | `omnihub read <note-url> --platform xiaohongshu --text` |
| Health check | `omnihub doctor --text` |
| Token-caching CLI | `xiaohongshu-cli search "<query>" --limit 5` (see reference) |

A note is readable by OmniHub only when its URL carries a valid `xsec_token`. Search results include the token; a bare note id usually does not. For cached-token and author-profile fallback workflows, use the bundled CLI:

```bash
xiaohongshu-cli search "西安 红叶" --limit 5
xiaohongshu-cli read <note-id> --author-url "<author-url>"
```

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. A note URL including the `xsec_token` query parameter (for example from a `search` result).

## Multi-source use

- Xiaohongshu is priority `170`; `xiaohongshu.com` / `xhslink.com` URLs route here automatically.
- `--mode aggregate` combines notes with web, WeChat, or Reddit results for broad research questions.
- `search --enrich N` reads the first `N` notes; because reads need tokens, enrichment works best on URLs returned by this platform's own search.
- Omit `--platform` for routing; pass it whenever the user names 小红书.

## Requirements

- Adapter: `daemon` only, plus `browser` capability.
- Browser: a logged-in Xiaohongshu session. Login is performed by the user; never automate the form.
- The standalone CLI additionally requires the `kimi-webbridge` daemon and supports `--daemon-url`.
- No API secret.

## Source order

1. `kimi-browser` search — `daemon`, priority `10`, call `browse`, script `scripts/search.js`, `when: adapter:daemon`, parse `json_items`.
2. `kimi-browser` read — `daemon`, priority `10`, call `browse`, script `scripts/read.js`, parse `daemon_page`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title, URL, author, and a snippet; read items include title, URL, author, and the note body.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon is not running; start it / connect the extension and retry. |
| `NEED_LOGIN` | Xiaohongshu requires a session; ask the user to sign in at <https://www.xiaohongshu.com>. |
| `PLATFORM_BLOCKED` | Verification / CAPTCHA page; ask the user to resolve it in the browser, do not loop. |
| `NOT_FOUND` | The note page has no content, or the note is deleted/private. |
| `TIMEOUT` | The feed did not load; retry once. |
| `BAD_INPUT` | The target is not a note URL. |

The standalone CLI uses additional codes (`missing_xsec_token`, `note_unavailable`, `token_not_found`) documented in the reference.

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.js` — browser script that extracts `section.note-item` results.
- `scripts/read.js` — browser script that extracts the note title, body, and author.
- `scripts/xiaohongshu_cli.py` — standalone token-caching CLI (console entry point `xiaohongshu-cli`).
- `references/xiaohongshu-cli.md` — full CLI flags, error codes, and workflows.
