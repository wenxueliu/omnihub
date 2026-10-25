---
name: xiaoyuzhou
description: Search Xiaoyuzhou podcasts, read an episode page, and transcribe episode audio with Whisper. Use when the user asks for 小宇宙 podcasts, wants an episode summary, or needs a podcast transcript. 中文触发词：小宇宙、播客、播客搜索、单集、转写、转录、听写。
---

# xiaoyuzhou

Searches Xiaoyuzhou podcast episodes through Exa, reads an episode page through Jina / the browser / Exa, and transcribes episode audio with a Whisper-compatible API. Transcription is the only Python implementation the platform ships.

## When to use

- The user wants podcasts: 「小宇宙上有什么关于 AI 的播客」「找一期讲 xxx 的单集」.
- The user shares an episode URL and wants its page content: 「读一下这期播客」.
- The user wants a transcript: 「把这期播客转成文字」「转录一下」.
- Not suitable for subscriber-only audio that is not publicly fetchable.

## Commands

| Goal | Command |
|---|---|
| Search episodes | `omnihub search "<query>" --platform xiaoyuzhou --limit 5 --text` |
| Read an episode page | `omnihub read <episode-url> --platform xiaoyuzhou --text` |
| Transcribe an episode | `omnihub transcribe <episode-url> --platform xiaoyuzhou --text` |
| Transcribe with options | `omnihub transcribe <url> --platform xiaoyuzhou --provider groq --language zh --polish --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Keywords.
- `limit` — `int`, default `5`, range `1–20`.

### `read`
- `target` — `str`, required, URL. An episode URL on `xiaoyuzhoufm.com`.

### `transcribe`
- `target` — `str`, required, URL. An episode URL, or a direct public audio URL.
- `provider` — `str`, default `auto`, choices `auto|groq|openai`. `auto` picks the first configured key.
- `model` — `str`, optional. Overrides the provider's default Whisper model.
- `language` — `str`, default `zh`. Spoken-language hint.
- `polish` — `bool`, default `false`. Ask Groq Llama to add Chinese punctuation and paragraphs.

## Multi-source use

- Xiaoyuzhou is priority `180`; `xiaoyuzhoufm.com` URLs route here automatically.
- `--mode aggregate` combines episode search with web and other platforms for topic research.
- `omnihub read <xiaoyuzhou-url>` never needs `--platform`; the domain match selects it.
- Pass `--platform xiaoyuzhou` when the user names 小宇宙.

## Requirements

- Adapters: `http` and `builtin`.
- Tools: `ffmpeg` for audio download/conversion during `transcribe`.
- Secrets: a Whisper API key for the chosen provider (`GROQ_API_KEY` or `OPENAI_API_KEY`, resolved by the builtin transcriber).
- No login or browser is required, though a browser source exists for reads.

## Source order

1. `exa` search — `http`, priority `10`.
2. `jina` read — `http`, priority `10`.
3. `kimi-browser` read — `daemon`, priority `20`, built-in `read` action.
4. `exa` read — `http`, priority `30`.
5. `whisper` transcribe — `builtin`, priority `10`, call `transcribe`, script `scripts/transcribe.py`, tool `ffmpeg`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Transcribe items carry the transcript text plus provider/model metadata.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | `ffmpeg` or an API key is missing; install/configure and retry. |
| `NEED_LOGIN` | Only for the browser read source; sign in or use Jina/Exa. |
| `PLATFORM_BLOCKED` | The page or audio host refused the request. |
| `NOT_FOUND` | The episode or audio URL does not exist. |
| `TIMEOUT` | Transcription exceeded the 300s budget; retry or use a shorter clip. |
| `UPSTREAM_BROKEN` | The provider returned an unexpected transcription payload. |
| `BAD_INPUT` | The target is not an episode/audio URL or an option is invalid. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/transcribe.py` — audio download, Whisper call, and optional polish step.
