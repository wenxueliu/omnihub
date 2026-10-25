# 迁移与新增平台指南

本仓库把 Agent-Reach 的“channel 类 + 静态注册表”重构为“平台目录 + 声明式
manifest + 共享执行核心”。一个平台就是一个目录：删掉目录即下线，新增目录
即上线，不需要改核心代码。

## 1. 从 Agent-Reach 迁移

### 1.1 结构对照

| 旧结构 | 新结构 |
|---|---|
| `agent_reach/channels/<name>.py` | `agent_reach/platforms/<id>/platform.yaml` |
| `Channel.name` | manifest `id`（必须与目录名一致） |
| channel 的 `search` / `read` 方法 | `actions.search` / `actions.read` |
| 代码里写死的后端顺序 | `sources[].priority`（越小越优先） |
| channel 内部的超时、重试、冷却 | 共享 `core/policy.py` 与 `core/ratelimit.py` |
| `channels/__init__.py` 静态列表 | 目录发现（内置 + 插件根） |
| 解析逻辑散落在 channel 内 | `parse/*.py` 或带夹具的平台 `parse.py` |
| `~/.agent-reach/state.json` | `~/.agent-reach/state/rate_limits.json` |

### 1.2 迁移步骤

1. **盘点旧 channel**：列出 action、参数、后端顺序、错误码映射、凭据。
2. **建目录**：`agent_reach/platforms/<id>/platform.yaml`，先声明 `id`、
   `description`、`level`、`priority`、`domains`、`capabilities`。
3. **搬来源**：每个后端一条 `sources`，保留原顺序为 `priority` 10/20/…。
   能用现有 adapter（`http` / `cli` / `opencli` / `daemon` / `builtin`）
   就不要写平台代码。
4. **搬解析**：通用 JSON/Markdown 走 `parse/*`；平台专用解析放
   `platforms/<id>/parse.py`（仅内置平台自动受信任）。
5. **声明检查**：加 `check.search` / `check.read`，让 `reach-platforms check`
   覆盖主路径。
6. **补测试**：`platforms/<id>/fixtures/` 放真实响应样本，至少一个
   search、一个 read；在 `tests/test_platform_contracts.py` 加契约断言。
7. **验收**：见第 4 节命令。
8. **删旧代码**：确认新路径工作后删除旧 channel 与静态注册项。

### 1.3 不要直接搬的东西

- `shell=True`、`load_dotenv`、`sys.path.insert`、仓库内相对状态文件。
- 旧的 channel 级冷却实现：新核心已经统一处理，重复实现会绕开
  `retry_after_seconds` 与跨进程账本。
- 上游未确认 provenance 的代码（见 `implementation.md` 12.5 节）。
- 旧 `~/.agent-reach/state.json` 的冷却条目不必迁移：它们会自然过期，
  迁移旧的暂停时间反而可能误伤新来源。

### 1.4 行为差异

- CLI 是子命令式：`search` / `read` / `check` / `cooldowns` / `platforms` /
  `doctor` / `explain` / `plugin` / `skill`；旧式
  `agent-reach <channel> <action>` 不在本仓库提供，需要兼容层时在调用方包一层。
- 输出 envelope 保持 `schema_version: 1`；新增字段只做向后兼容扩展
  （如 `error.retry_after_seconds`、`attempts[].retry_after_seconds`）。
- 退出码：成功 `0`，`BAD_INPUT` 与参数/配置错误 `2`，其他失败 `1`。
- `RATE_LIMITED` / `PLATFORM_BLOCKED` 是终止码：不会继续切平台。

## 2. 新增一个平台

最小闭环只需要一个 manifest：

```yaml
id: example
description: Example site search and reads.
version: 1
level: default
priority: 200
capabilities: []

domains: [example.com, www.example.com]

requires:
  adapters: [http]

check:
  search: {query: "hello", limit: 1}
  read: {target: "https://example.com/"}

actions:
  search:
    params:
      query: {type: str, required: true}
      limit: {type: int, default: 5, minimum: 1, maximum: 20}
    sources:
      - adapter: http
        name: jina
        url: https://r.jina.ai/https://example.com/search?q=${query|urlencode}
        parse: [jina]
        priority: 10
  read:
    params:
      target: {type: str, format: url, required: true}
    sources:
      - adapter: http
        name: jina
        url: https://r.jina.ai/${target}
        parse: [jina]
        priority: 10
```

要点：

- `capabilities` 必须声明；纯声明式平台写 `[]`。
- 可选来源用 `when: secret:NAME`、`when: tool:NAME` 或
  `when: adapter:NAME`；条件不满足记为 `SKIP`。
- URL 参数写 `format: url`，会经过共享 SSRF 策略校验。
- 需要限速时在来源上加 `min_interval`（秒），核心最多等待 10s。
- 草稿阶段可以先不写 `check`；`check --dry-run` 会把它计入 `skipped`。

## 3. 删除或禁用平台

- **删除**：直接删 `agent_reach/platforms/<id>/`。registry、`platforms`、
  `skill`、`check` 都会自动不再包含它。
- **临时禁用**：在 `routing.yaml` 写 `platforms: {<id>: {enabled: false}}`，
  或给来源加 `disabled: true`；目录保留，便于恢复。
- **改名**：目录名、`id`、routing 里的引用必须一起改。

## 4. 验收命令

```bash
# 离线
python -m pytest -q
python -m agent_reach.cli plugin validate ~/.agent-reach/plugins
python -m agent_reach.cli doctor --text
python -m agent_reach.cli check --dry-run --text

# 联网（显式）
python -m agent_reach.cli check --platform <id> --text
python -m agent_reach.cli search "<query>" --platform <id> --limit 3 --text
python -m agent_reach.cli read "<url>" --platform <id> --text
```

新平台合入前至少满足：

- [ ] manifest 通过 schema 与跨文件引用校验（`platforms` 无 invalid）
- [ ] `doctor` 中该平台为 `ok`；缺凭据只应是 `unknown` 而非 error
- [ ] `check --platform <id>` 的 `search` 与 `read` 均为 `ok`
      （若因冷却被跳过，先按 `retry_after_seconds` 等待，或确认上游恢复后
      `cooldowns reset --platform <id>`）；确有短时凭据的读取可只声明
      `check.search` 并在文档说明原因
- [ ] 至少一个 search fixture 与一个 read fixture
- [ ] 删除目录后其余平台的测试仍然通过

## 5. 登录/浏览器依赖平台的迁移结论（2026-10-25 更新）

twitter、xueqiu、xiaohongshu、xiaoyuzhou、linkedin **已完成迁移**；
facebook、instagram、boss 明确不迁移。

最终选择的工具不是 OpenCLI，而是本仓库已有的 kimi-webbridge `daemon`
适配器。新增的 `browse` 复合动作把“导航 → 等待选择器 → 执行站点 JS →
限流/登录标记”封装为 manifest 可声明的来源；站点选择器仍留在各平台目录，
核心不知道任何站点细节。只有 builtin 或显式信任的平台可以调用 `browse`。

| 平台 | 旧动作 | 新来源（2026-10-25 实测） | 迁移结论 |
|---|---|---|---|
| twitter (X) | search, profile, tweets, thread | daemon `browse`：`x.com/search?f=live` 提取 `article`；详情页读 `article`。Exa `site:x.com` 搜索兜底 | 已迁移（search/read；profile/tweets/thread 未提供） |
| xueqiu | quote, search(股票), hot_posts, hot_stocks | daemon `browse`：`/k?q=` 提取 `.timeline__item`；状态页读 `h1` + `.article__bd__detail` | 已迁移（search/read） |
| xiaohongshu | search, read, comments | daemon `browse`：`search_result` 提取 `section.note-item`；详情读 `#detail-title` / `#detail-desc` | 已迁移（search/read；comments 未提供） |
| xiaoyuzhou | 转写 / 外部搜索 | Exa `site:xiaoyuzhoufm.com` 搜索；Jina → daemon → Exa 读取；页面音频 → ffmpeg 压缩切片 → Groq/OpenAI Whisper 转写 | 已迁移（search/read/transcribe） |
| linkedin | jobs, read | daemon `browse` 内调用 public guest API（搜索 + 职位详情） | 已迁移（search/read） |
| facebook | search, profile | 仍仅 OpenCLI | 不迁移 |
| instagram | search(用户), profile, posts | 仍仅 OpenCLI | 不迁移 |
| boss | search, read | OpenCLI + boss-agent-cli（CDP 9222） | 不迁移 |

迁移前置条件（后续新增同类平台时仍适用）：

- 用户亲自在连接的浏览器里完成站点登录；本仓库不接收密码或 Cookie。
- 站点 JS 只从已加载页面读取查询词（`location` / `URLSearchParams`），
  manifest 不把 `${query}` 插值进 `code`，避免脚本注入。
- 登录重定向用 `login_url` 或 `{login:true}` 标记；验证码/风控返回
  `{blocked:true}`；两者分别映射 `NEED_LOGIN` / `PLATFORM_BLOCKED`。
- 用 `explain` 确认来源顺序，并让 `check --platform <id>` 的 search/read
  至少各通过一次；小红书的 `xsec_token` 会过期，只对搜索做 live check。
- 在 `docs/release.md` 记录实际 backend、登录方式与已知限制。

复用边界：只借来源顺序与错误分类思路。不要复制带凭据轮换、CDP 端口假设或
provenance 不清晰的代码；第三方 API（Groq、Metaso）需要用户自备 Key，且要
在 README 与 `THIRD_PARTY.md` 中写清配额与许可。

## 6. 工具型平台补充（2026-10-25）

`~/.agents/skills/` 下的 `deepwiki` 与 `notebooklm` 按同一模型新增为平台，
但不属于上面的 OpenCLI 迁移：

| 原 skill | 新平台 | 来源 |
|---|---|---|
| deepwiki（MCP 工具） | `deepwiki` (200) | builtin hook POST 公开端点 `mcp.deepwiki.com/mcp`，调用 `read_wiki_contents` |
| notebooklm（CLI） | `notebooklm` (210) | builtin hook 调 `notebooklm list/summary --json` |

取舍：

- DeepWiki 的 MCP 服务只提供 `read_wiki_structure` / `read_wiki_contents` /
  `ask_wiki_question`，没有搜索工具，因此平台只声明 `read`；不引入
  `npx @seflless/deepwiki`，避免额外的 Node 依赖。
- NotebookLM 的 CLI 没有服务端标题过滤，`search` 在本地按标题/id 过滤；
  `read` 用 `summary --json --topics`，URL 先归一为 notebook id。CLI 属于
  可选工具：缺失时来源 `SKIP`、`doctor` 为 `degraded`，`check` 将该检查记为
  `skipped` 而不是 `failed`。
