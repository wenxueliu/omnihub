---
name: v2ex
description: Search V2EX topics and read a topic with its replies. Use when the user asks what V2EX thinks about a topic, wants to find discussions, or shares a V2EX topic URL. 中文触发词：V2EX、V站、主题搜索、帖子回复、看看大家怎么说。
---

# v2ex

Searches V2EX topics through the SOV2EX index (with an Exa fallback) and reads a topic plus its replies. The topic reader is the only code this platform ships.

## When to use

- The user wants community opinion: 「V2EX 上大家怎么看 xxx」「搜一下 V 站的讨论」.
- The user shares a `v2ex.com/t/<id>` URL and wants the thread: 「读一下这个帖子」「把回复也整理出来」.
- Not suitable for content that requires an account or a login-only board.

## Commands

| Goal | Command |
|---|---|
| Search topics | `omnihub search "<query>" --platform v2ex --limit 5 --text` |
| Read a topic | `omnihub read https://www.v2ex.com/t/1 --platform v2ex --text` |
| Read by id | `omnihub read 1 --platform v2ex --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–50`.

### `read`
- `target` — `str`, required. A V2EX topic URL or a numeric topic id.

## Multi-source use

- V2EX is priority `120`; explicit `v2ex.com` URLs route here before the public-web fallback.
- `--mode aggregate` combines V2EX with Reddit / web results for 「多个社区怎么看」 questions.
- Omit `--platform` for automatic routing; pass it when the user names V2EX or V站.

## Requirements

- Adapters: `http` and `builtin`.
- No secret, login, or browser.

## Source order

1. `sov2ex` — `http`, priority `10`, sorted by creation time.
2. `exa` — `http`, priority `20`, site-restricted fallback.
3. `v2ex-api` — `builtin` read, call `read_topic`, script `scripts/read.py`, priority `10`.
4. `scripts/parse.py` — search response parser declared as `parse_script`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include title, URL, author, reply count, and creation time; read items include the topic body and replies.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | A required adapter or builtin script is unavailable. |
| `RATE_LIMITED` | SOV2EX or V2EX is throttling; retry later. |
| `NOT_FOUND` | The topic id does not exist or was deleted. |
| `UPSTREAM_BROKEN` | The search index or topic page changed shape. |
| `BAD_INPUT` | The target is not a V2EX URL or numeric id. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/parse.py` — SOV2EX search parser.
- `scripts/read.py` — topic and reply reader.
