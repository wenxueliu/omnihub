---
name: youtube
description: Search YouTube videos and read a video page's transcript-style content through Exa and the browser. Use when the user asks for YouTube videos, tutorials, or the content of a watch link. 中文触发词：YouTube、油管、视频搜索、教程视频、看这个视频、视频内容。
---

# youtube

Searches YouTube through Exa and reads a watch page through Exa or the logged-in browser. It is fully declarative and ships no scripts.

## When to use

- The user wants videos: 「YouTube 上有什么 asyncio 教程」「找几个讲 xxx 的视频」.
- The user shares a `youtube.com` / `youtu.be` link and wants its page content: 「看看这个视频讲了什么」.
- Not suitable for exact subtitle extraction; Bilibili has that for B站, while YouTube reads return the page/transcript-style text that Exa or the browser can see.

## Commands

| Goal | Command |
|---|---|
| Search videos | `omnihub search "<query>" --platform youtube --limit 5 --text` |
| Read a video page | `omnihub read <watch-url> --platform youtube --text` |
| Health check | `omnihub doctor --text` |

`youtu.be` short links are accepted by `read`.

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. A YouTube watch URL or `youtu.be` short link.

## Multi-source use

- YouTube is priority `140`; `youtube.com` / `youtu.be` URLs route here automatically.
- `--mode aggregate` combines video results with web and Reddit for broad learning queries.
- Omit `--platform` for routing; pass it when the user names YouTube.

## Requirements

- Adapters: `http` (Exa) and optionally `daemon` (browser read).
- No secret or login. Some videos are region- or age-gated and may only be readable in the browser.

## Source order

1. `exa` search — `http`, priority `10`, site-restricted to `youtube.com`.
2. `exa` read — `http`, priority `10`.
3. `kimi-browser` read — `daemon`, priority `20`, `when: adapter:daemon`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include video title, URL, author, and snippet; read items carry the page text available to the backend.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | No usable adapter; check the OmniHub install. |
| `PLATFORM_BLOCKED` | The video is region/age-gated or YouTube refused the backend. |
| `NOT_FOUND` | The video was removed or the URL is wrong. |
| `TIMEOUT` | The page did not settle; retry once. |
| `UPSTREAM_BROKEN` | Exa returned an unexpected response shape. |
| `BAD_INPUT` | The target is not a YouTube URL. |

## Bundled files

- `platform.yaml` — actions, parameters, and sources. This platform ships no scripts.
