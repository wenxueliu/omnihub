---
name: linkedin
description: Search LinkedIn jobs and read a job posting through the public guest API in the browser. Use when the user asks for job openings, wants LinkedIn job results, or shares a LinkedIn job URL. 中文触发词：领英、LinkedIn、招聘、职位搜索、岗位、职位详情。
---

# linkedin

Searches LinkedIn jobs and reads a posting through LinkedIn's public guest endpoints, executed in the user's browser. The guest API is used so no account automation is required; the browser supplies a normal session and cookies.

## When to use

- The user wants job listings: 「搜一下 LinkedIn 上的 Python 岗位」「上海有什么数据工程职位」.
- The user shares a `linkedin.com/jobs/view/<id>` URL and wants the description: 「读一下这个职位」.
- Not suitable for recruiter-only or authenticated member content.

## Commands

| Goal | Command |
|---|---|
| Search jobs | `omnihub search "<keywords>" --platform linkedin --limit 10 --text` |
| Read a posting | `omnihub read <job-url> --platform linkedin --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Job keywords.
- `location` — `str`, optional, default empty. Location filter. The `omnihub` CLI does not expose this flag; pass it through the Python API (`run_action(..., params={"query": ..., "location": ...})`) or a caller that supplies platform params directly.
- `limit` — `int`, default `10`, range `1–25`.

### `read`
- `target` — `str`, required, URL. A LinkedIn job posting URL containing the numeric job id.

## Multi-source use

- LinkedIn is priority `190`; `linkedin.com` URLs route here automatically.
- `--mode aggregate` can combine it with the public web when the user wants both listings and company context.
- Omit `--platform` for routing; pass it when the user names LinkedIn or 领英.

## Requirements

- Adapter: `daemon`.
- Browser: any LinkedIn session. `/authwall` or `/login` redirects surface as `NEED_LOGIN`.
- No API secret.

## Source order

1. `kimi-browser` search — `daemon`, priority `10`, call `browse`, script `scripts/search.js`.
2. `kimi-browser` read — `daemon`, priority `10`, call `browse`, script `scripts/read.js`.

## Output

One JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Search items include job title, URL, company, location, and posting time; read items carry the full job description.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The daemon is not running; start it and retry. |
| `NEED_LOGIN` | LinkedIn redirected to `/authwall` or `/login`; ask the user to sign in. |
| `PLATFORM_BLOCKED` | The guest API returned a non-OK status; back off. |
| `NOT_FOUND` | The job id is invalid or the posting was removed. |
| `TIMEOUT` | The page did not load; retry once. |
| `BAD_INPUT` | The target is not a LinkedIn job URL. |

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.js` — guest job search parser.
- `scripts/read.js` — guest job posting parser.
