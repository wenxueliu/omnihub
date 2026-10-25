---
name: bilibili
description: Search Bilibili (B站/哔哩哔哩) videos and read a video's subtitles. Use when the user asks to search B站, find or read a Bilibili/bilibili video, or extract a video's subtitle transcript. 中文触发词：B站、哔哩哔哩、bilibili、视频搜索、字幕、视频字幕。
---

# bilibili

Searches Bilibili videos through the public search API and reads a video's subtitle tracks through the user's logged-in browser. This skill is a thin wrapper over `platform.yaml`; the same platform also participates in OmniHub's multi-source routing.

## When to use

- The user wants to find Bilibili videos: 「搜一下 B 站」「bilibili 上有什么视频」「找几个哔哩哔哩教程」.
- The user shares a Bilibili video URL and wants its title, metadata, or subtitle text: 「读一下这个 B 站视频」「提取这个视频的字幕」.
- Not suitable when the video has no subtitle tracks, or when the browser is not logged in — Bilibili only serves subtitles to a logged-in session.

## Commands

| Goal | Command |
|---|---|
| Search videos | `omnihub search "<query>" --platform bilibili --limit 5 --text` |
| Read a video | `omnihub read <url> --platform bilibili --text` |
| Read subtitles only | `omnihub read <url> --platform bilibili` (the `items` entry carries the transcript) |
| Health check | `omnihub doctor --text` |
| Explain routing | `omnihub explain --platform bilibili --action search` |

`b23.tv` short links are accepted by `read` and are followed in the browser.

## Parameters

### `search`
- `query` — `str`, required. Keywords; GitHub/Bilibili-style qualifiers are passed through to the API.
- `limit` — `int`, default `5`, range `1–20`. Maximum number of videos.

### `read`
- `target` — `str`, required, URL. A Bilibili video URL (`bilibili.com/video/...`, `m.bilibili.com/video/...`) or a `b23.tv` short link.

## Multi-source use

- Omit `--platform` and OmniHub routes by `priority` (bilibili is `110`) and the caller's intent.
- `--mode aggregate` runs several platforms concurrently and merges results; Bilibili's results can be combined with web, YouTube, or Reddit when the user asks a broad question.
- `read <bilibili-url>` is routed here automatically by the `bilibili.com` / `b23.tv` domain match.
- `search --enrich N` reads the body of the first `N` search hits — useful for 「搜索并总结」 requests.
- Pass `--platform bilibili` whenever the user explicitly names B站.

## Requirements

- Adapters: `http` (public video search) and `daemon` (subtitle read through the browser).
- Browser: for `read`, a logged-in Bilibili session in the Kimi Browser Extension daemon. If the daemon is down, start it / connect the extension, then retry.
- No API secret is required.

## Source order

1. `bilibili-api` — `builtin`, priority `5`, call `search`, script `scripts/search.py`. Public API search.
2. `exa` — `http`, priority `10`. Site-restricted fallback (`site:bilibili.com`).
3. `kimi-browser` — `daemon`, priority `10`, call `browse`, script `scripts/read.js`, guarded by `when: adapter:daemon`. Subtitle read.
4. `subtitles` parser — platform parser `scripts/parse.py`, used for the browser JSON payload.

## Output

Every CLI invocation prints one JSON envelope: `ok`, `channel`, `action`, `backend`, `items`, `meta`, and `error` when `ok` is false. `--text` renders a human-readable form. Search items carry `title`, `url`, `author`, `text`, and source metadata; read items carry the video metadata and the resolved transcript.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon or a required adapter is unavailable; start it and retry. |
| `NEED_LOGIN` | The browser is not logged in to Bilibili; ask the user to sign in, never automate the login. |
| `PLATFORM_BLOCKED` | Bilibili served a verification or anti-bot page; resolve it manually, do not loop retries. |
| `RATE_LIMITED` | Back off and retry later with a larger `min_interval`. |
| `NOT_FOUND` | The video, cid, or subtitle track does not exist. |
| `TIMEOUT` | The browser did not finish loading; retry once, then fall back to another platform. |
| `UPSTREAM_BROKEN` | Bilibili's API shape changed; report it rather than guessing. |
| `BAD_INPUT` | The URL is not a Bilibili video or a valid search query. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/parse.py` — Bilibili-specific response and subtitle parsers.
- `scripts/search.py` — builtin public API search.
- `scripts/read.js` — browser script that collects subtitle tracks.
- `fixtures/` — recorded payloads used by the test suite.
