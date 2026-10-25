---
name: noop
description: Minimal offline reference platform used by OmniHub contract tests. Use only when developing or testing OmniHub itself, not for real user research. 中文触发词：noop、测试平台、占位平台、契约测试。
---

# noop

A deliberately minimal, offline platform. It returns deterministic placeholder data so the manifest, adapter, pipeline, and output-contract tests can run without network access. It is not intended for real research.

## When to use

- Writing or debugging OmniHub core code and you need a stable platform that never touches the network: 「用 noop 测一下」「跑契约测试」.
- Verifying a new adapter or parser against a known payload shape.
- Not suitable for any real search or read; results are placeholders.

## Commands

| Goal | Command |
|---|---|
| Search | `omnihub search "anything" --platform noop --limit 3 --text` |
| Read | `omnihub read example.com --platform noop --text` |
| Health check | `omnihub doctor --text` |

## Parameters

### `search`
- `query` — `str`, required. Echoed into the placeholder result.
- `limit` — `int`, default `3`. Maximum number of placeholder items.

### `read`
- `target` — `str`, required. URL or id echoed into the placeholder result.

## Multi-source use

- `level: experimental` and `priority: 999`, so routing normally avoids it.
- `public_web: false`; it never acts as a public-web fallback.
- It is only selected when explicitly requested with `--platform noop`, which makes it safe for tests.

## Requirements

- Adapter: `builtin` only.
- No network, secret, login, browser, or external tool.

## Source order

1. `noop` search — `builtin`, priority `10`, call `noop_search`, script `scripts/search.py`.
2. `noop` read — `builtin`, priority `10`, call `noop_read`, script `scripts/read.py`.

## Output

The standard JSON envelope (`ok`, `channel`, `action`, `backend`, `items`, `meta`, `error`); `--text` for humans. Items are deterministic placeholders keyed by the input.

## Failure codes

| Code | Meaning / next step |
|---|---|
| `NEED_SETUP` | The builtin scripts are missing; reinstall the package. |
| `BAD_INPUT` | A required parameter is absent or malformed. |

Network, login, rate-limit, and upstream codes never apply to this platform.

## Bundled files

- `platform.yaml` — actions, parameters, sources, scripts.
- `scripts/search.py` — deterministic placeholder search.
- `scripts/read.py` — deterministic placeholder read.
