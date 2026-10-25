# Third-party code

This project is MIT-licensed. The following upstream projects are either
copied, adapted, or planned for reuse. Their licenses apply to the derived
files; this file records provenance so future changes remain reviewable.

## Agent-Reach

- Upstream: `reference/Agent-Reach`
- License: MIT, Copyright (c) 2025 Agent Eyes
- Status: copied and adapted in M0; execution and web backends adapted in M1; GitHub/V2EX/Bilibili platform logic adapted in M2

Copied files:

| Target | Source | Changes |
|---|---|---|
| `omnihub/core/result.py` | `agent_reach/runtime/result.py` | added `BUSY` constant, optional item provenance, `meta` merge |
| `omnihub/core/clock.py` | `agent_reach/runtime/clock.py` | none |
| `omnihub/utils/url.py` | `agent_reach/utils/url.py` | none |
| `omnihub/utils/text.py` | `agent_reach/utils/text.py` | none |
| `omnihub/utils/process.py` | `agent_reach/utils/process.py` | none |

M1 adaptations:

| Target | Source | Changes |
|---|---|---|
| `omnihub/core/context.py` | `agent_reach/runtime/run.py` | subprocess logic moved behind `Context.run`; bounded stdout/stderr readers; process-group timeout kill; argv-only (no shell); added HTTP client with redirect re-validation, secret lookup and output caps |
| `omnihub/adapters/opencli.py` | `agent_reach/backends/opencli.py` | kept YAML `ok: false` error-block detection and status mapping; invocation rebuilt as explicit argv with `-f json`; no shell |
| `omnihub/parse/web.py` | `agent_reach/channels/web.py` | Exa JSON-RPC/SSE payload extraction, Jina Reader header/content parsing and anti-bot detection; moved from the web platform to a shared parser module |
| `omnihub/adapters/cli.py` | `agent_reach/runtime/run.py` | command construction separated from execution; mapping helpers only |

M2 adaptations:

| Target | Source | Changes |
|---|---|---|
| `omnihub/platforms/github/parse.py`, `read.py` | `agent_reach/channels/github.py` | kept the repository-search path, target parsing, raw README accept header, issue/PR comments and metadata fields. Replaced the `gh` backend with manifest HTTP sources; token only from `GH_TOKEN` / `GITHUB_TOKEN` via `ctx.secret` (no config-file fallback); URL-encoded owner/repo; all size/timeout limits come from `Context` |
| `omnihub/platforms/v2ex/parse.py`, `read.py` | `agent_reach/channels/v2ex.py` | kept topic-id parsing, `topics/show.json` + `replies/show.json`, `unix_to_iso` and node/member metadata. Search is new (SOV2EX, since the public API has no search); transport goes through `Context.http` instead of urllib plus a curl fallback |
| `omnihub/platforms/bilibili/parse.py` | `agent_reach/channels/bilibili.py` | kept the AI/CC subtitle item shape (title, author, publish time, cue text) and Bilibili API error mapping. The `bili-cli` transport is gone: the browser source resolves `cid` and the signed subtitle URL, and the parser downloads the JSON through `ctx.http` with a Bilibili Referer. No cookie store or `BILIBILI_COOKIE` |

No upstream copyright notices were removed.

## union-search-skill

- Upstream: `reference/union-search-skill`
- License: MIT, Copyright (c) 2026 Union Search Skill contributors
- Status: patterns adapted in M1; no module copied wholesale

Adapted in M1:

| Target | Source | Notes |
|---|---|---|
| `parse/generic.py` JSON/YAML salvage | `scripts/cli/adapters.py` | tolerant item extraction from noisy tool stdout; no import-time side effects |

Remaining candidates (adapt away from subprocess/`sys.path`/`.env` side
effects before use):

| Target | Source | Notes |
|---|---|---|
| item normalization/dedupe | `scripts/union_search/structured_results.py` | remove `lxml` dependency |
| rate-limit policy | `scripts/channels/no_api/request_state.py` | inject storage path; map cooldowns to `RATE_LIMITED` |
| no-API HTML platforms | `scripts/channels/no_api/**` | manifest-driven, fixtures, no import-time `.env` |
| `platforms/web` API sources | `scripts/channels/{exa,jina,tavily,serper,kimi}/**` | extract request logic only |
| URL→Markdown | `scripts/url_to_markdown/engines/{jina,firecrawl,defuddle}*` | lazy imports; Node dependency stays optional |

### Do not copy without provenance review

- `scripts/channels/ai_tools/native_search/` — upstream `THIRD_PARTY.md`
  records migration from a local project with no verifiable public repository
  or license.
- `scripts/union_image_search/` and media downloaders — out of scope.
- `scripts/channels/exa/exa_mcp.py` — uses `mcporter` through `shell=True`;
  reuse only the output/error salvage pattern, never the shell invocation.

### Import-time side effects to remove during reuse

- `load_dotenv()` at module import in most channel modules.
- `sys.path.insert(...)` bootstrapping in `union_search.py` and `scripts/cli/*`.
- module-level `logging.basicConfig(force=True)`, shared `_STATE`, sessions and
  log directories.

## kimi-webbridge

Not part of union-search-skill. `scripts/channels/kimi/kimi_search.py` is a
direct Moonshot HTTP client and must not be used as the daemon adapter. The
daemon contract is documented in `docs/implementation.md` and comes from the
user-local `kimi-webbridge` skill; no source is vendored here.

## Python dependencies

- PyYAML — MIT
- jsonschema — MIT
- requests — Apache-2.0
