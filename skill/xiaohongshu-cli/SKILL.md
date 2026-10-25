---
name: xiaohongshu-cli
description: Use when the user wants to search or read Xiaohongshu (小红书) notes. Invoke when the user mentions 小红书/Xiaohongshu, asks to search notes or comments there, or wants the body of a Xiaohongshu note.
---

# Xiaohongshu CLI

Searches and reads Xiaohongshu notes in the user's real, logged-in browser through the Kimi Browser Extension daemon (`kimi-webbridge`). Read-only: it does not like, comment, follow, or post.

## Requirements

- The `kimi-webbridge` skill. If a command fails because the daemon is unreachable or the extension is not connected, recover the way that skill describes, then retry. If the daemon is not on its default address, pass `--daemon-url http://<host>:<port>`.
- The CLI. Either install this repo (`pip install -e .`) and run `xiaohongshu-cli`, or run it from the repo root as `python -m omnihub.xiaohongshu_cli` with the working directory set to `/home/chengnanfeng/code/harness/services/omnihub`.
- A logged-in browser. Run `xiaohongshu-cli login-status` first. If it reports `not_logged_in`, ask the user to sign in at <https://www.xiaohongshu.com> in that browser, then retry. Never automate the login form.

## Commands

| Command | Args / Flags | Returns |
|---------|--------------|---------|
| `login-status` | — | `{logged_in, user: {nickname, user_id}}` |
| `search` | `<query> [--limit N] [--scroll N]` | `{query, count, items: [{id, title, author, author_id, author_url, url, xsec_token, raw_text}]}` |
| `read` | `<id-or-url> [--token T] [--author-url U] [--refresh]` | `{id, url, title, text, author, author_url, date, tags, stats, token_source}` |

Run `xiaohongshu-cli <command> --help` for full flag documentation.

- `--limit` defaults to 10 (1–50). `--scroll` defaults to 1 and only matters when the first page has fewer notes than `--limit`.
- `read` accepts a note id (`6addcd87000000002c00cb5a`) or any note URL. A URL that already carries `xsec_token` is used as-is.
- `--author-url` is the fallback token source when a cached token expires. Take it from a `search` result (`author_url`), including its own `xsec_token`.
- `--refresh` ignores the cached token and re-resolves it from the author profile.

## Output format

Every command prints one JSON envelope on stdout:

```json
{"ok": true, "data": ...}
```

On error (non-zero exit):

```json
{"ok": false, "error": {"code": "error_code", "message": "human-readable message"}}
```

Error codes worth branching on:

| Code | Meaning / next step |
|------|---------------------|
| `daemon_unreachable` | Start the kimi-webbridge daemon / connect the extension, then retry |
| `not_logged_in` | Ask the user to sign in in the browser |
| `site_unreachable` | The browser cannot load Xiaohongshu; check network/VPN |
| `platform_blocked` | Verification/CAPTCHA page; ask the user to resolve it in the browser, do not retry in a loop |
| `missing_xsec_token` | Run `search` first, or pass `--token` / `--author-url` |
| `note_unavailable` | The token expired or the note is gone; `search` again for a fresh token |
| `token_not_found` | The note is not on the author profile (deleted or private) |
| `no_results` | The query returned no notes |
| `bad_input` | Invalid id/URL/flag |

## Common workflows

```bash
# Search, then read the first result
xiaohongshu-cli search "西安 红叶" --limit 5
xiaohongshu-cli read 6addcd87000000002c00cb5a

# A note seen in an earlier run: the token is cached, so id is enough
xiaohongshu-cli read <note-id>

# The cached token expired and the note is no longer in search results
xiaohongshu-cli read <note-id> --author-url "https://www.xiaohongshu.com/user/profile/<uid>?xsec_token=...&xsec_source=pc_search"

# Force a fresh token even if one is cached
xiaohongshu-cli read <note-id> --refresh --author-url "<author-url>"
```

## Known limitations

- Login is done by the user in the browser; the CLI never automates it.
- Note detail pages require a short-lived `xsec_token`. `search` captures and caches tokens in `$XDG_CACHE_HOME/xiaohongshu-cli/notes.json` (file mode 0600, no query text). Tokens expire; when one does, re-run `search` or pass `--author-url`.
- Xiaohongshu search ranking changes between runs, so a note from an earlier run may not appear again. The cache and author-profile fallback cover that case if the author URL is known.
- The CLI stops on verification/CAPTCHA pages (`platform_blocked`); resolve them manually in the browser.
- Extraction is selector-based (`section.note-item`, `#detail-title`, `#detail-desc`). If Xiaohongshu changes its DOM, the commands may return `no_results` or empty text.
- Video notes return the caption/text, not a transcript.
