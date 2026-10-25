# reach-platforms

配置驱动、按平台组织的 Agent 互联网访问层。

当前完成 **M4** 并进入 **M5**：M0 契约层（目录发现、manifest 校验、两级
优先级路由）+ M1 执行层（HTTP/CLI/OpenCLI 适配器、`web` 公共网页平台）+
M2 平台批次一（`github`、`v2ex`、`bilibili`）+ M3 有状态工具与按需抓取
（kimi-webbridge `daemon` 适配器、`search --enrich N`）+ M4 插件校验与
信任、`explain` 来源明细、`SKILL.md` 生成、平台批次二（`reddit`、
`youtube`）+ M5 平台自声明的 `check` live 回归与统一限流/冷却 + 平台批次三
（`twitter`、`xueqiu`、`xiaohongshu`、`xiaoyuzhou`、`linkedin`，浏览器
`browse` 复合动作）+ 工具型平台（`deepwiki` 公开 MCP、`notebooklm` 本地
CLI）。CLI 已支持 `platforms`、`doctor`、`explain`、`plugin`、
`skill`、`check`、`cooldowns`、`search` 和 `read`。剩余平台、MCP 适配器详见
`docs/implementation.md`。

## 快速开始

```bash
cd services/reach-platforms
python -m pytest -q

# 列出发现的平台（JSON 是默认输出）
python -m agent_reach.cli platforms

# 解释一次请求会走哪些平台
python -m agent_reach.cli explain --platform web --action search
python -m agent_reach.cli explain --url https://www.example.com/a --action read

# 搜索与按需读取
python -m agent_reach.cli search "yt-dlp github" --platform web --limit 3
python -m agent_reach.cli read https://example.com --platform web

# 搜索后自动补正文（失败隔离，失败项进 meta.enrich_errors）
python -m agent_reach.cli search "example domain" --limit 3 --enrich 2

# 站点平台
python -m agent_reach.cli search "flask" --platform github --limit 3
python -m agent_reach.cli read https://github.com/pallets/flask/issues/42 --text
python -m agent_reach.cli search "python 限流" --platform v2ex --limit 3
python -m agent_reach.cli read https://www.v2ex.com/t/1 --text
python -m agent_reach.cli search "rust 教程" --platform bilibili --limit 3
python -m agent_reach.cli read https://www.bilibili.com/video/BV1rpWjevEip --text
python -m agent_reach.cli search "asyncio" --platform reddit --limit 3
python -m agent_reach.cli read https://www.reddit.com/r/Python/comments/j531n0/an_introduction_to_async_programming_python/ --text
python -m agent_reach.cli search "asyncio tutorial" --platform youtube --limit 3
python -m agent_reach.cli read https://www.youtube.com/watch?v=oAkLSJNr5zY --text

# 登录态平台（需要本机 kimi-webbridge daemon 与浏览器登录）
python -m agent_reach.cli search "openai" --platform twitter --limit 3
python -m agent_reach.cli read https://x.com/user/status/1 --text
python -m agent_reach.cli search "茅台" --platform xueqiu --limit 3
python -m agent_reach.cli search "咖啡" --platform xiaohongshu --limit 3
python -m agent_reach.cli search "科技" --platform xiaoyuzhou --limit 3
python -m agent_reach.cli search "python" --platform linkedin --limit 3
python -m agent_reach.cli read https://www.linkedin.com/jobs/view/4379639599/ --text

# 工具型平台
python -m agent_reach.cli read facebook/react --platform deepwiki --text
python -m agent_reach.cli search "asyncio" --platform notebooklm --limit 3
python -m agent_reach.cli read nbb0987654321 --platform notebooklm --text

# 检查工具、适配器、凭据与登录提示（不发起网络请求）
python -m agent_reach.cli doctor --text

# 平台自声明的 live 回归（--dry-run 只列出计划，不联网）
python -m agent_reach.cli check --dry-run --text
python -m agent_reach.cli check --platform github --platform reddit --text

# 查看 / 清除本地冷却账本（显式操作，不会自动绕过限流）
python -m agent_reach.cli cooldowns --text
python -m agent_reach.cli cooldowns reset --platform github --text

# 第三方插件：只校验、不执行；生成给 Agent 用的 SKILL.md
python -m agent_reach.cli plugin validate ~/.agent-reach/plugins
python -m agent_reach.cli plugin list --text
python -m agent_reach.cli skill --output SKILL.md

# 离线契约示例平台
python -m agent_reach.cli search hello --platform noop --limit 2 --text

# 安装后也可用 reach-platforms 或 agent-reach
pip install -e .
reach-platforms platforms --text
```

### 小红书 kimi-webbridge CLI

除了平台化的 `search/read`，仓库还提供了一个可独立使用的小红书 CLI。它在
用户已登录的真实浏览器里执行搜索、捕获笔记的 `xsec_token`、再读取正文，
并提供作者主页兜底与 token 缓存：

```bash
# 安装后使用 xiaohongshu-cli；未安装则用 python -m
python -m agent_reach.xiaohongshu_cli login-status
python -m agent_reach.xiaohongshu_cli search "西安 红叶" --limit 5
python -m agent_reach.xiaohongshu_cli read 6addcd87000000002c00cb5a
python -m agent_reach.xiaohongshu_cli read <note-id> --author-url "<作者主页 URL>"
```

输出统一为 `{"ok": true, "data": ...}`，错误时为
`{"ok": false, "error": {"code", "message"}}` 并以非零码退出。token 缓存在
`$XDG_CACHE_HOME/xiaohongshu-cli/notes.json`（0600，不保存查询词）。使用说明
见 [`skill/xiaohongshu-cli/SKILL.md`](skill/xiaohongshu-cli/SKILL.md)。

`web` 是公共网页兜底平台：搜索走 Exa hosted MCP（无需 Key），读取依次
尝试 Jina Reader、kimi-webbridge 真实浏览器（daemon 可用时）、Exa。它的
默认优先级是 10，因此普通 `search` 默认走公共网页；站点平台（`github`
100、`bilibili` 110、`v2ex` 120、`reddit` 130、`youtube` 140、`twitter`
150、`xueqiu` 160、`xiaohongshu` 170、`xiaoyuzhou` 180、`linkedin` 190、
`deepwiki` 200、`notebooklm` 210）通过 `--platform`，或 routing 里自定义的
`intents` 使用。`read` 按 URL 域名
先选站点平台，再自动把 `web` 作为最后一个来源。

- `github`：REST 搜索仓库；读取仓库（含 README）、issue、PR。设置
  `GITHUB_TOKEN` 或 `GH_TOKEN` 可提高限额并访问私有仓库。
- `v2ex`：SOV2EX 搜索主题，V2EX 公共 API 读取主题和第一页回复。
- `bilibili`：Exa `site:bilibili.com` 搜索；读取优先 Jina，配置
  `BILIBILI_COOKIE` 后可用视频 API 获取结构化元数据。
- `reddit`：Exa `site:reddit.com` 搜索；Exa fetch 读取公开帖子与高赞评论，
  daemon 可用时可用真实浏览器兜底（Reddit 的匿名 JSON 与 Jina 都返回 403）。
- `youtube`：Exa `site:youtube.com` 搜索；Exa fetch 读取页面正文/字幕文本，
  daemon 可用时可用真实浏览器兜底。
- `twitter` / `xueqiu` / `xiaohongshu` / `linkedin`：搜索与读取都通过
  kimi-webbridge 在用户已登录的真实浏览器里执行站点 JavaScript。平台目录
  只保存选择器与提取逻辑，`browse` 复合动作负责导航、等待选择器、登录/风控
  标记和结果限流。daemon 不可用时这些平台记为 `degraded`，不会让 `doctor`
  失败。Twitter 另有 Exa `site:x.com` 搜索兜底。
- **标签页生命周期**：每次 `read` / `browse` 使用独立 session，结束后（成功、
  失败或超时）都会关闭该 session 的全部标签页；并发查询不会互相关闭对方的
  标签，超时恢复也只接管 `reach:` 分组内的标签，不会动用户自己打开的页面。
- `xiaoyuzhou`：Exa `site:xiaoyuzhoufm.com` 搜索；读取优先 Jina，daemon
  可用时用真实浏览器兜底，最后再走 Exa fetch。
- 小红书的读取链接必须带搜索结果里的 `xsec_token`；token 会过期，因此
  `check` 只对搜索做 live 回归。
- `deepwiki`：读取公开仓库的 DeepWiki 文档。直接 POST 官方 MCP 端点
  `read_wiki_contents`，无需 Node 或 API Key；接受 `owner/repo`、GitHub
  仓库 URL 或 deepwiki.com URL。未索引的仓库返回 `NOT_FOUND` 并提示先在
  deepwiki.com 打开一次。
- `notebooklm`：搜索与读取本机 Google NotebookLM。`search` 用
  `notebooklm list --json` 列出并按标题过滤，`read` 用
  `notebooklm summary --json --topics` 返回摘要和建议问题。需要本机安装
  `notebooklm` CLI（`pip install notebooklm-py`）并已完成 `notebooklm
  login`；CLI 缺失时平台标记为 `degraded`，来源自动 `SKIP`。

## 目录

```
agent_reach/
  cli.py                 # platforms / doctor / explain / check / cooldowns / search / read
  xiaohongshu_cli.py     # kimi-webbridge 小红书 CLI（login-status / search / read）
  core/
    config.py            # 分层 routing 配置
    context.py           # 子进程、HTTP、secret；超时/输出上限
    doctor.py            # 工具/适配器/凭据/登录态体检（不联网）
    normalize.py         # item 归一、URL 规范化、聚合去重
    pipeline.py          # 路由 → 来源降级 → 解析 → envelope
    policy.py            # 超时、输出上限、URL 安全、错误分类
    registry.py          # 扫描/校验/索引 platform.yaml
    result.py            # Item / envelope / error codes（复用 Agent-Reach）
    router.py            # 显式平台 / URL / intent / priority
    schema/              # JSON Schema
    template.py          # ${param} / ${secret:NAME} / 过滤器模板
    enrich.py            # search --enrich N：并发补正文、失败隔离
    plugins.py           # 第三方插件校验（不执行代码）
    skill.py             # 由 registry 生成 SKILL.md
  adapters/
    base.py              # Adapter / ToolCall / RawResult 协议
    builtin.py           # 平台目录内可信 Python hooks
    http.py / cli.py / opencli.py
    daemon.py            # kimi-webbridge：/status、session、cursor 分页、browse 站点脚本
  parse/
    generic.py           # JSON / YAML / OpenCLI / text 解析链
    web.py               # 共享 Exa / Jina 解析器
    daemon.py            # 浏览器读取结果 → 统一 item
  platforms/noop/        # 最小离线平台（契约测试/示例）
  platforms/web/         # Exa + Jina 公共网页平台
  platforms/github/      # GitHub 搜索、仓库/issue/PR 读取
  platforms/v2ex/        # SOV2EX 搜索、主题与回复读取
  platforms/bilibili/    # Exa 搜索、Jina / Cookie API 读取
  platforms/reddit/      # Exa 搜索与帖子读取、浏览器兜底
  platforms/youtube/     # Exa 搜索与视频页/字幕读取、浏览器兜底
  platforms/twitter/     # 登录态浏览器搜索与推文读取（Exa 搜索兜底）
  platforms/xueqiu/      # 登录态浏览器讨论搜索与状态读取
  platforms/xiaohongshu/ # 登录态浏览器笔记搜索与读取
  platforms/xiaoyuzhou/  # Exa 搜索、Jina/浏览器/Exa 读取
  platforms/linkedin/    # 浏览器内 guest API 职位搜索与职位详情
  platforms/deepwiki/    # 公开 MCP 读取仓库 DeepWiki 文档
  platforms/notebooklm/  # 本地 notebooklm CLI：笔记本搜索与摘要
  utils/                 # URL、文本脱敏、子进程环境（复用 Agent-Reach）
```

## 平台即插件

每个平台是一个目录，`platform.yaml` 是唯一必需文件。删除目录即下线，
不需要改核心代码。声明式平台直接引用共享适配器；需要代码时，平台目录可
提供 `search.py` / `read.py` / `parse.py`（仅内置平台默认受信任）。

```yaml
id: example
description: Example platform
level: default            # default | optional | experimental
priority: 20              # 数字越小越优先
domains: [example.com]

actions:
  search:
    params:
      query: {type: str, required: true}
      limit: {type: int, default: 5}
    sources:
      - adapter: cli
        name: example-cli
        tool: example
        call: search
        args: {query: "${query}", limit: "${limit}"}
        parse: json_items
        priority: 10
```

来源按 `priority` 升序尝试；`disabled: true` 可单独关闭一个来源。
平台级和来源级优先级互相独立。`first_success` 遇到空结果或非终态错误会
继续下一个来源；`NEED_LOGIN`、`RATE_LIMITED`、`PLATFORM_BLOCKED`、
`BAD_INPUT` 等终态错误会立即停止。

可选来源用 `when: secret:NAME`、`when: tool:NAME` 或
`when: adapter:NAME` 声明：条件不满足时记为 `SKIP`，不会让整个平台失败。
`adapter:daemon` 表示“该适配器的健康探针通过才运行”，例如 `web` 的
kimi-webbridge 读取来源。平台的 `auth.secret` 表示“可选登录凭据”，缺失时
`doctor` 提示但仍使用公共来源；`requires.secrets` 则表示必需凭据。

### 第三方插件

插件放在 `$REACH_PLUGIN_PATH`、`~/.agent-reach/plugins/<id>/` 或
`./.agent-reach/plugins/<id>/`，同名覆盖内置平台。纯清单插件直接加载；
带 `search.py` / `read.py` / `parse.py` 的插件是在用户权限下执行 Python，
因此必须声明 `capabilities`（`http` / `exec` / `browser`，纯计算写 `[]`）
并且显式信任：

```bash
python -m agent_reach.cli plugin validate ~/.agent-reach/plugins
python -m agent_reach.cli search "query" --trust-plugin my-plugin
# 或 REACH_TRUSTED_PLUGINS=my-plugin
```

未信任时插件仍会出现在 `platforms` 里，但运行其代码会返回 `NEED_SETUP`；
`plugin validate` 会给出提醒。`explain` 会列出每个平台的来源顺序、
`adapter`、`when` 与 `parse`，`skill` 从 registry 生成文档，两者都不会
输出凭据或来源 `args`。

### 限流与冷却

失败会写入跨进程的冷却账本，下一次请求在真正发出网络/命令调用前就会跳过：

| 错误码 | 范围 | 默认时长 |
|---|---|---|
| `RATE_LIMITED` | 整个平台（所有来源与动作） | 300s，优先使用 `Retry-After`，上限 6h |
| `PLATFORM_BLOCKED` | 整个平台 | 900s |
| `TIMEOUT` / `UPSTREAM_BROKEN` / `NEED_SETUP` | 单个来源 | 60s / 120s / 60s |

`PLATFORM_BLOCKED` 表示验证码或风控，`RATE_LIMITED` 是终止码：不会切到
下一个平台。单来源暂停只跳过该来源，同平台其他来源和其他平台仍可继续。
被跳过的尝试在 `meta.attempts[]` 里带 `result: SKIP`、`reason` 和
`retry_after_seconds`；最终错误也会在 `error.retry_after_seconds` 中给出
剩余秒数。成功调用会清除该来源的暂停；平台冷却只会自然过期。

账本位于 `~/.agent-reach/state/rate_limits.json`（目录 0700、文件 0600），
可用 `REACH_STATE_DIR` 改写位置。文件只保存平台/来源名与时间戳，不保存
查询、URL 或凭据；损坏或不可写时直接忽略，不会阻塞请求。

`doctor` 会在对应平台下输出 `cooling` 行；`cooldowns` 命令列出全部未过期
条目，`cooldowns reset [--platform ID]` 在确认上游已恢复后显式清除。重置
不影响 `min_interval` 的 pacing 时间戳，也不会自动绕过限流。`check` 把只被
账本跳过（没有真正发起调用）的项标为 `cooling`，不计入失败；网络返回的
429 仍记为失败。

平台可在来源上声明 `min_interval`（秒），用于连续调用同一来源时的最小
间隔；核心把它限制在 10s 以内，超出的部分只会等待，不会报错：

```yaml
sources:
  - adapter: http
    name: exa
    min_interval: 1.5
```

## 路由配置

按以下顺序合并，后者覆盖前者：

1. 内置默认值
2. `~/.agent-reach/config/routing.yaml`
3. `./.agent-reach/config/routing.yaml`
4. `$REACH_ROUTING`
5. `--routing FILE`

```yaml
version: 1
mode: first_success        # first_success | aggregate

intents:
  code: [github, web]

platforms:
  github: {enabled: true, priority: 5}

aggregate:
  top: 3
  per_platform_limit: 5
  dedupe: true
```

安全策略（超时上限、输出上限、URL 白名单与 DNS 解析校验）不在 routing
配置里，平台不能通过 YAML 放宽这些限制。发起 HTTP 前会拒绝解析到
loopback/私网地址的公网域名，重定向后同样复检。凭据通过 `${secret:NAME}` 从环境变量或
`~/.agent-reach/secrets.yaml` 注入，不写进 manifest 输出。

## 设计文档

- [`docs/implementation.md`](docs/implementation.md) — 完整设计、契约、迁移计划
- [`docs/migration.md`](docs/migration.md) — 从旧 channel 迁移、新增/删除平台
- [`docs/release.md`](docs/release.md) — 0.1.0 发布验收记录与已知限制
- [`THIRD_PARTY.md`](THIRD_PARTY.md) — 复用代码与许可

## License

MIT。复用的上游代码版权与许可见 `LICENSE` 和 `THIRD_PARTY.md`。
