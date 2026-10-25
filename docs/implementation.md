# OmniHub：从 Agent Reach 到平台化访问层（实现文档）

- **状态**：M0–M5 已落地；278 项离线测试、21 项 live check 通过
- **日期**：2026-10-25
- **基线**：Agent-Reach v1.6.0（`reference/Agent-Reach`，只读参考）
- **建议落地仓库**：`services/omnihub/`（独立 git 仓库）
- **许可**：Agent-Reach 为 MIT；若复制其代码，需保留 LICENSE 与版权声明

---

## 1. 背景与目标

Agent-Reach 目前是“一个平台一个 Python 类”的硬编码模型：

- 平台注册在 `agent_reach/channels/__init__.py:24-39` 的静态列表，新增平台必须改代码。
- 动作的后端顺序写死在 `Action.backends`（`agent_reach/channels/base.py:30-49`），用户无法配置。
- `runtime/tools.py:52-68` 的工具表只负责“安装与路径解析”，不描述“怎么调用、怎么解析输出”。
- `runtime/public_web.py:56-64` 把公共网页兜底写死为 `search/jobs/read` 三种动作名。
- 配置只有扁平 key + 白名单（`runtime/manage.py:954`），无法覆盖平台顺序、工具顺序、超时。
- 没有目录扫描或 entry point 发现机制，第三方无法在不改核心的情况下加平台。

**目标**：

1. 平台是插件与删除边界，一个平台一个自包含目录。
2. 每个平台提供 `search`、`parse`、`read` 三项能力。
3. 平台之间、平台内部工具之间都由**配置优先级**决定顺序。
4. 搜索结果与正文读取分离：默认只返回链接与摘要，正文按需读取。
5. 加/删平台不改中心注册表，`omnihub platforms` 与 SKILL.md 自动反映变化。
6. 保持现有 CLI 语法、JSON envelope、错误码兼容，支持渐进迁移。

**非目标（首版不做）**：

- 不把 YAML 做成图灵完备的脚本语言。
- 不在首版重写全部平台；按平台逐个迁移。
- 不引入新的写操作（发帖、评论、点赞、私信）。
- 不改变“默认免 Key、只读、不替用户登录”的产品原则。

---

## 2. 定稿决策

| 决策 | 结论 | 理由 |
|---|---|---|
| 组织方式 | **按平台分** | 高内聚、可整块删除；工具是平台内部可替换的执行器 |
| 搜索模式 | 默认 `first_success`，`--mode aggregate` 可选 | 默认低延迟、结果可控；聚合按需开启 |
| 平台实现 | **声明式优先** + 目录内可选 Python | 简单平台只写 `platform.yaml`；复杂平台在目录内放 `search.py/parse.py/read.py` |
| 优先级 | **平台级 + 平台内来源级** | 既能配“先问哪个平台”，也能配“平台内先走哪个工具” |
| 配置格式 | YAML（复用 `pyyaml`） | 现有依赖已有，可读性优于 JSON |
| 兼容策略 | 保持 CLI 语法、envelope、错误码；渐进迁移 | 现有使用方零感知 |
| 正文抓取 | 默认不自动抓；`--enrich N` 或 Agent 显式 `read` | 避免 N+1 请求与不必要的反爬风险 |
| 落地位置 | `services/omnihub/` | `reference/` 只读，不能就地修改 |

---

## 3. 架构总览

```
Agent
  │
  ▼
omnihub search / read / platforms / explain
  │
  ▼
Router（平台优先级 + intent 配置）
  ├── platforms/bing_cn/     search → parse → read
  ├── platforms/github/      search → parse → read
  ├── platforms/bilibili/    search → parse → read
  └── platforms/web/         兜底
         │
         ▼
   shared adapters: http | cli | opencli | daemon | mcp
   shared policy:   auth | rate-limit | timeout | URL 安全 | 错误分类
```

**分层原则**：

- **平台层**：拥有业务语义（这个站点怎么搜、字段怎么映射、正文怎么读）。
- **适配器层**：只负责“怎么调一个工具/接口”，不理解平台语义。
- **核心层**：路由、管道、策略、统一结果，不为任何平台写特例。
- **平台可以依赖适配器，适配器不能依赖平台。**

---

## 4. 核心契约

### 4.1 Item（统一结果）

沿用现有 `make_item`（`runtime/result.py:85-102`）：

```python
{
    "title": str | None,
    "url": str | None,
    "author": str | None,
    "published_at": str | None,   # ISO 8601
    "text": str | None,           # 摘要或正文
    "raw": Any,                   # 原始字段，便于追溯
}
```

搜索结果额外带：

```python
{
    "platform": str,      # 产出该条的平台 id
    "source": str,        # 平台内来源，如 bili-cli / opencli / http
    "priority": int,      # 命中的优先级
}
```

### 4.2 Envelope（统一响应）

沿用 `runtime/result.py:116-142`，`schema_version = 1`：

```json
{
  "schema_version": 1,
  "ok": true,
  "channel": "bilibili",
  "action": "search",
  "backend": "bili-cli",
  "items": [],
  "meta": {
    "elapsed_ms": 123,
    "attempts": [{"backend": "bili-cli", "result": "OK"}]
  },
  "error": null
}
```

多平台聚合时：

- `channel` 为 `"@aggregate"`（或保留第一个平台，另在 `meta.platforms` 列出）。
- 每条 item 必须带 `platform`。
- `meta.attempts` 记录每个平台/来源的结果。

### 4.3 错误码

沿用 `runtime/result.py:23-33`：

`BAD_INPUT | NEED_SETUP | NEED_LOGIN | NOT_FOUND | TIMEOUT | RATE_LIMITED | PLATFORM_BLOCKED | BUSY | UPSTREAM_BROKEN`

- 适配器负责把上游错误映射到这套码。
- `FINAL_CODES` 仍然终止 fallback：`NOT_FOUND / BAD_INPUT / PLATFORM_BLOCKED / RATE_LIMITED / NEED_LOGIN / BUSY`。

### 4.4 Platform Protocol

```python
class Platform(Protocol):
    manifest: PlatformManifest

    def search(self, query: str, *, limit: int, params: dict, ctx: Context) -> list[RawItem]:
        """调用一个或多个工具，返回原始结果。"""

    def parse(self, raw: RawResult, *, action: str, ctx: Context) -> list[Item]:
        """把原始结果映射为统一 Item；解析失败抛 OmniHubError。"""

    def read(self, target: str, *, params: dict, ctx: Context) -> Item:
        """读取一个 URL 的正文/详情。"""
```

- 声明式平台由核心根据 `platform.yaml` 自动生成这三个方法。
- 复杂平台在目录内提供同名 Python 模块，核心优先使用。
- `parse` 必须可独立测试（输入 fixture，输出 Item）。

### 4.5 Adapter Protocol

```python
class Adapter(Protocol):
    kind: str

    def probe(self) -> Health:
        """工具是否可用、版本、登录态。"""

    def invoke(self, call: ToolCall, ctx: Context) -> RawResult:
        """执行一次调用，返回原始输出或结构化错误。"""
```

`ToolCall`：

```python
@dataclass
class ToolCall:
    tool: str
    action: str
    args: dict
    argv: list[str] | None = None
    body: dict | None = None
    output: str = "json"          # json | yaml | text | markdown
```

`RawResult`：

```python
@dataclass
class RawResult:
    ok: bool
    data: Any
    text: str | None
    error: OmniHubError | None
    meta: dict                    # 耗时、字节数、命令、退出码
```

---

## 5. 平台包规范

### 5.1 目录结构

```
omnihub/platforms/<id>/
  platform.yaml        # 必需：身份、动作、来源、优先级、依赖
  search.py            # 可选：复杂搜索
  parse.py             # 可选：平台专用解析器
  read.py              # 可选：正文/详情读取
  fixtures/            # 原始响应样本（解析测试用）
  tests/               # 契约测试 + 解析测试
```

**目录即插件**：registry 扫描 `platforms/*/platform.yaml`，删除目录即下线。

### 5.2 `platform.yaml`

```yaml
id: bilibili
description: B站视频
level: default                 # default | optional | experimental
priority: 20                   # 数字越小越优先
domains: [bilibili.com, b23.tv]

requires:
  tools: [bili-cli]            # 缺失时 add 会安装
  adapters: [cli, opencli, http]

auth:
  session: bili-cli
  login: ["bili", "login"]
  login_url: https://passport.bilibili.com/login

actions:
  search:
    params:
      query: {type: str, required: true}
      limit: {type: int, default: 5}
    sources:
      - adapter: cli
        tool: bili-cli
        call: search
        args: {query: "${query}", limit: "${limit}"}
        parse: json:bilibili.video
        priority: 10
      - adapter: opencli
        site: bilibili
        args: {query: "${query}", limit: "${limit}"}
        parse: opencli:generic
        priority: 20
      - adapter: http
        url: "https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=${query}"
        parse: json:bilibili.api
        priority: 30
    public_web:
      scopes: [bilibili.com/video/]
      priority: 90

  read:
    params:
      target: {type: str, required: true}
    sources:
      - adapter: cli
        tool: bili-cli
        call: video
        args: {target: "${target}"}
        parse: json:bilibili.video
        priority: 10
      - adapter: http
        url: https://r.jina.ai/${target}
        parse: markdown
        priority: 90
    timeout: 30

  subtitle:
    params:
      target: {type: str, required: true}
    sources:
      - adapter: cli
        tool: bili-cli
        call: subtitle
        args: {target: "${target}"}
        parse: text
```

### 5.3 Python 扩展

只有声明不够时才使用。核心按约定加载：

```python
# omnihub/platforms/bilibili/parse.py
def parse(raw, *, action, ctx):
    ...
```

```python
# omnihub/platforms/bilibili/search.py
def search(query, *, limit, params, ctx):
    ...
```

```python
# omnihub/platforms/bilibili/read.py
def read(target, *, params, ctx):
    ...
```

规则：

- 函数签名固定，返回统一 `Item`。
- 不允许直接读 `.env`；凭据通过 `ctx.secret(key)` 获取。
- 不允许绕过 `ctx.http` / `ctx.run_cmd`，否则会失去超时、大小上限、URL 安全与错误分类。
- 平台目录内可以放私有辅助模块，但不能 import 其他平台。

---

## 6. 工具与适配器规范

### 6.1 工具清单

```yaml
# omnihub/tools/opencli.yaml
id: opencli
kind: cli
install:
  manager: npm
  package: "@jackwener/opencli"
  singleton: true
  floor: "1.8.7"
  exclude: ["1.8.8"]
  tested: "1.8.7"
  needs: [node]
invoke:
  argv: ["opencli", "${site}", ...]
  output: json
  error_parser: yaml-envelope
limits:
  timeout: 30
  max_output_bytes: 8388608
capabilities: [search, read, browser]
```

### 6.2 适配器清单

| kind | 覆盖工具 | 关键实现点 |
|---|---|---|
| `http` | 直接 API、Jina、Exa、curl 场景 | 复用 `http_request`；代理、大小上限、SSRF、重定向复检 |
| `cli` | `gh`、`bili`、`yt-dlp`、curl 命令 | argv 数组、cwd=home、进程组超时、输出上限 |
| `opencli` | OpenCLI 站点工具 | `opencli <site> ... -f json`；错误实际是 YAML，需兼容 |
| `daemon` | kimi-webbridge | `POST 127.0.0.1:10086/command`，body `{action,args,session}`，返回 `{ok,data\|error}`；需 `/status` 健康检查、cursor 分页 |
| `mcp` | Exa hosted MCP | JSON-RPC，处理 SSE `data:` 行 |
| `browser` | Cloak/OpenCLI 登录态 | 只提供会话与 Cookie，不做平台语义 |

### 6.3 kimi-webbridge 的适配要点

- 它是**常驻 loopback daemon**，不是一次性 CLI：`/home/chengnanfeng/.kimi-webbridge/bin/kimi-webbridge start` 后，`POST http://127.0.0.1:10086/command`。
- 请求：`{"action": "navigate|snapshot|click|fill|evaluate|...", "args": {...}, "session": "<task>"}`。
- 响应：`{"ok": true, "data": ...}` 或 `{"ok": false, "error": {"code", "message"}}`。
- `extension_not_connected` 表示 daemon 在、浏览器扩展不在；映射为 `NEED_SETUP` 或 `NEED_LOGIN`，不要当成平台失败。
- `snapshot` 支持 `max_chars`、`truncated`、`next_cursor`，cursor 5 分钟过期；适配器需要分页循环。
- 地址可能变化：读 `~/.kimi-webbridge/config.json` 或 `/status`，不要硬编码端口。

### 6.4 输出解析链

统一按顺序尝试：

1. JSON（标准 envelope）
2. YAML（OpenCLI 错误块）
3. 纯文本 / Markdown
4. 平台专用 parser

解析器命名：

- `json:<name>`：JSON 字段映射
- `yaml-envelope`：识别 `ok: false`
- `html:<name>`：CSS/XPath
- `markdown`
- `text`

所有 parser 必须有 fixture 测试。

---

## 7. 管道语义

### 7.1 search

```
1. Router 解析平台顺序
2. 对每个平台：
   a. 按 sources[].priority 顺序尝试
   b. adapter.invoke()
   c. platform.parse()
   d. 空结果 / 可重试错误 → 下一个 source
   e. FINAL_CODES → 停止并返回错误
3. first_success：第一个有结果的平台直接返回
4. aggregate：前 K 个平台并发执行，合并、去重、排序
```

### 7.2 parse

- 每个 source 声明 `parse`。
- 平台可以覆盖通用 parser。
- 解析失败视为该 source 失败，继续 fallback。
- 解析结果必须经过 `normalize_item()`：URL 规范化、时间 ISO 化、字段裁剪。

### 7.3 read

```
1. 按域名匹配平台（can_handle）
2. 平台 read 的 sources 按 priority 尝试
3. 无平台匹配 → web 兜底
4. 内容可读性检查（登录墙/验证码/空壳）
```

### 7.4 按需抓正文

两种模式：

```bash
omnihub search "query"                 # 只返回链接 + 摘要
omnihub search "query" --enrich 3      # 自动读取前 3 条
omnihub read <url>                     # Agent 自己挑
```

`--enrich` 规则：

- 只处理 `text` 为空或过短的 item。
- 并发上限默认 3，单条超时独立。
- 单条失败不拖垮整体，失败信息进 `meta.enrich_errors`。
- 抓取前重新做 URL 安全校验。

---

## 8. 路由与优先级

### 8.1 两级优先级

1. **平台间**：先问哪个平台。
2. **平台内来源**：先走哪个工具。

### 8.2 解析顺序

```
--platform 显式指定
  > intent 配置
  > 平台 manifest 的 priority
  > 目录名兜底
```

### 8.3 `routing.yaml`

```yaml
version: 1
mode: first_success            # first_success | aggregate

intents:
  default: [bing_cn, baidu, duckduckgo, web]
  code:    [github, web]
  video:   [bilibili, youtube, web]
  social:  [xiaohongshu, weibo, zhihu, web]
  news:    [bing_cn, baidu, web]

platforms:
  github:      {enabled: true, priority: 5}
  bing_cn:     {enabled: true, priority: 10}
  bilibili:    {enabled: true, priority: 20}
  xiaohongshu: {enabled: false}

aggregate:
  top: 3
  per_platform_limit: 5
  dedupe: true
```

### 8.4 配置分层

```
内置 manifest 默认值
  └─ ~/.omnihub/config/*.yaml
       └─ ./.omnihub/config/*.yaml
            └─ 环境变量 / secrets
                 └─ CLI 参数
```

合并规则：

- 平台级：`enabled`、`priority`、`intents` 可覆盖。
- 来源级：按 `id` 覆盖或新增；`disabled: true` 可关闭单个来源。
- 不允许用户配置覆盖安全策略（URL 白名单、输出上限、超时下限）。

---

## 9. 目录结构

```
omnihub/
  core/
    registry.py            # 扫描/加载/校验平台与工具清单
    router.py              # 平台优先级、intent、first_success/aggregate
    pipeline.py            # search → parse → read、来源降级、attempts
    normalize.py           # Item 规范化、去重、排序
    policy.py              # 超时、输出上限、URL 安全、限流、错误分类
    result.py              # Item / envelope / error codes
    config.py              # 分层配置合并
    schema/
      platform.schema.json
      tool.schema.json
      routing.schema.json
  adapters/
    base.py                # Adapter Protocol / ToolCall / RawResult
    http.py  cli.py  opencli.py  daemon.py  mcp.py
  parse/
    generic.py             # JSON/YAML/HTML/regex 通用解析
  platforms/
    web/  github/  v2ex/  bilibili/  ...
  tools/
    *.yaml
  cli.py
```

用户侧：

```
~/.omnihub/
  config/
    routing.yaml
    platforms/<id>.yaml     # 覆盖内置平台
    tools/<id>.yaml
  plugins/<id>/             # 第三方平台包
  state.json
  sessions/
```

---

## 10. 插件发现、删除与依赖

### 10.1 发现顺序

1. 包内 `omnihub/platforms/*/platform.yaml`
2. 用户插件 `~/.omnihub/plugins/*/platform.yaml`
3. 可选：Python entry point `omnihub.plugins`

同名时用户目录覆盖内置，`extends: builtin:<id>` 表示增量覆盖。

### 10.2 删除

- 删除平台目录即下线；registry 只扫描存在的目录。
- 不维护中心 import 列表。
- `omnihub platforms` 与 SKILL.md 自动更新。

### 10.3 依赖

```yaml
requires:
  tools: [bili-cli]
  adapters: [cli, http]
  platforms: [web]
```

- 加载时校验；缺失时 `doctor` 报具体缺什么。
- 删除被依赖的平台时，`doctor` 提示受影响平台，而不是运行时神秘失败。
- `web` 建议作为核心内置兜底，不允许插件删除。

---

## 11. 安全与策略

必须共享，不能在平台里各写一套：

- **命令执行**：argv 数组，禁止 `shell=True`；cwd 默认 home；超时后杀进程组。
- **URL**：所有请求走 `normalize_public_http_url`；重定向后复检；阻止私有 IP、userinfo、非 http(s)。发起请求前还会解析主机名，要求每个解析结果都是全局可路由地址，阻止 `localtest.me` 这类指向 loopback 的公网域名；解析失败按 `UPSTREAM_BROKEN` 处理。
- **输出上限**：HTTP 与子进程都有字节上限，超出部分截断并排空管道。
- **凭据**：`ctx.secret(key)`；日志与错误消息脱敏；不把 Cookie/Token 放进 `raw`。
- **限流**：平台级 cooldown（`RATE_LIMITED` / `PLATFORM_BLOCKED`）+ 来源级 pause（`TIMEOUT` / `UPSTREAM_BROKEN` / `NEED_SETUP`），状态跨进程持久化；来源可声明 `min_interval`，核心限制上限。
- **daemon**：只允许 loopback；校验地址来自本地配置文件；不对外暴露。
- **插件信任**：纯清单插件可直接加载；带 Python 代码的插件必须显式 `--trust-plugin <id>`（或 `OMNIHUB_TRUSTED_PLUGINS`），并声明所需能力（`http` / `exec` / `browser`）。

---

## 12. 兼容性

- 保留 `agent-reach <platform> <action>` 语法与 JSON envelope。
- 保留错误码与 `meta.attempts`。
- 旧 `Channel` 类通过 `LegacyChannelAdapter` 包装，行为不变。
- `channels/__init__.py` 的静态列表逐步清空，只留目录发现。
- 迁移期间两套实现可并存，按平台切换。
- `schema_version` 保持 1；新增字段只做向后兼容扩展。

---

## 12.5 代码复用清单

原则：**复用传输、解析、安全、错误分类这类横切能力；不复用静态平台表、
`if/elif` 分发、`sys.path` 引导和 import 时读 `.env`**。平台边界内保持
manifest + 少量 Python，共享能力全部走 `core/*` / `adapters/*`。

### 已落地（M0）

| 目标 | 来源 | 处理 |
|---|---|---|
| `core/result.py` | `reference/Agent-Reach/agent_reach/runtime/result.py` | 原样复制后：补 `BUSY` 常量、Item 可选 `platform/source/priority`、`envelope(meta=...)` |
| `core/clock.py` | `reference/Agent-Reach/agent_reach/runtime/clock.py` | 原样复制 |
| `utils/url.py` | `reference/Agent-Reach/agent_reach/utils/url.py` | 原样复制；SSRF/私有地址/userinfo 校验 |
| `utils/text.py` | `reference/Agent-Reach/agent_reach/utils/text.py` | 原样复制；URL 凭据脱敏 |
| `utils/process.py` | `reference/Agent-Reach/agent_reach/utils/process.py` | 原样复制；子进程 UTF-8 |
| `core/registry.py` 数据模型 | `reference/union-search-skill/scripts/cli/registry.py`、`union_search/platform_metadata.py` | 借 `PlatformCapability` 的字段划分（kind/auth/requires_env），但改为扫描目录，不保留 53 项静态表 |
| `parse/generic.py` JSON salvage | `reference/union-search-skill/scripts/cli/adapters.py` | 重写为无副作用函数，增加 fenced/前后日志容错 |
| `parse/generic.py` item 归一 | `reference/union-search-skill/scripts/union_search/structured_results.py` | 保留字段映射思路，去掉 `lxml`，统一到 `make_item` |
| `core/router.py` 聚合语义 | `reference/union-search-skill/scripts/union_search/union_search.py` | 只借去重/并发/deadline 思路；`first_success` 与 intent 路由是新实现 |

### 已落地（M1）

| 目标 | 来源 | 处理 |
|---|---|---|
| `core/context.py` | Agent-Reach `runtime/run.py`；union-search `channels/ai_tools/adapter.py` | 有界子进程（进程组超时、输出上限、UTF-8）、HTTP 重定向逐跳复检、大小上限、`ctx.secret` |
| `adapters/cli.py` | 同上 | 只接受 argv 数组；`tool/call/args` 映射为 `--key value`；无 `shell=True` |
| `adapters/http.py` | Agent-Reach `runtime/run.py::http_request`、`channels/web.py::read` | manifest 声明 URL/method/headers/body；JSON body 编码；URL 安全校验 |
| `adapters/opencli.py` | Agent-Reach `backends/opencli.py` | `opencli <site> ... -f json`；保留 YAML `ok: false` 错误块映射 |
| `adapters/builtin.py` | 新实现 | 只加载 `origin == builtin` 的平台 Python；第三方插件暂不执行 |
| `core/pipeline.py` | 新实现 | source 降级、`FINAL_CODES` 立即停止、`meta.attempts`、first_success / aggregate |
| `core/normalize.py` | union-search `structured_results.py` 思路 | item 归一、URL 规范化、聚合去重 |
| `core/template.py` | 新实现 | `${param}` / `${secret:NAME}`；纯占位符保留原生类型 |
| `platforms/web` | Agent-Reach `channels/web.py`（Exa search、Jina/Exa read） | 拆成 manifest sources + `parse.py`；保留 antibot 检测与错误映射 |
| `platforms/noop` Python hooks | 新实现 | 离线端到端测试 |

### 已落地（M2）

| 目标 | 来源 | 处理 |
|---|---|---|
| `platforms/github` | Agent-Reach `channels/github.py` | REST 搜索与仓库/README/issue/PR 读取；去掉 `gh` 依赖，token 只经 `ctx.secret` |
| `platforms/v2ex` | Agent-Reach `channels/v2ex.py` | 保留 topic/replies API 与字段映射；新增 SOV2EX 搜索 |
| `platforms/bilibili` | Agent-Reach `channels/bilibili.py` | 搜索走 Exa；`read` 改为在登录态浏览器里解析 `cid` 与字幕签名地址，再由 `parse.py` 下载字幕 JSON |
| `core/doctor.py` | 新实现 | 工具、适配器、必需/可选凭据、builtin auth.status；不联网、不泄露值 |
| `core/pipeline.py` source `when` | 新实现 | `secret:NAME` / `tool:NAME`；不满足记为 `SKIP` |
| `core/template.py` 过滤器 | 新实现 | `urlencode` / `lower` / `upper` / `strip`；查询值安全入 URL |
| `omnihub/parse/web.py` | M1 `platforms/web/parse.py` | Exa/Jina 解析器改为共享命名解析器 |

### 已落地（M3）

| 目标 | 来源 | 处理 |
|---|---|---|
| `adapters/daemon.py` | `~/.claude/skills/kimi-webbridge/SKILL.md` 协议说明 | 按外部契约实现；不复用 `kimi_search.py`（那是 Moonshot 直连 API） |
| `core/context.py::loopback_http` | 新实现 | 与公开 `http` 分离的 loopback 专用入口；地址、路径、凭据、重定向全部收紧 |
| `core/enrich.py` | 新实现 | 并发上限、失败隔离、URL 复检；不改 envelope 顶层语义 |
| source `when: adapter:NAME` | 新实现 | 复用适配器 `probe()`；探针异常按 SKIP 处理 |

### M2 复用候选

| 目标 | 来源 | 必要改造 |
|---|---|---|
| `core/ratelimit.py` | union-search `channels/no_api/request_state.py` + Agent-Reach `runtime/state.py` | 只借语义（平台冷却 / 来源暂停 / pacing），改为 JSON 账本、路径注入、0600/0700 原子写、fail open；429/captcha 映射 `RATE_LIMITED` / `PLATFORM_BLOCKED` |
| no-API 平台族 | union-search `channels/no_api/**` | 每个平台一个目录 + fixtures；去掉 import 时 `.env` |
| URL→Markdown | union-search `url_to_markdown/engines/*` | 懒加载可选依赖；Defuddle/Node 失败时降级 Jina |
| HTML/regex 通用解析 | union-search 各平台解析器 | 优先放进平台 `parse.py`；通用链只保留无依赖的 regex |

### 明确不复用

- `union_search.py` 的 `PLATFORM_MODULES` 静态表、`if/elif` 单平台分发、
  `sys.path.insert` 引导。
- 任何 `shell=True` 的调用（包括 `exa_mcp.py` 的 `mcporter` 包装）。
- `scripts/channels/ai_tools/native_search/`：上游 `THIRD_PARTY.md` 标注为
  无可验证公开仓库/许可证的迁移代码，未经 provenance review 不引入。
- `scripts/channels/kimi/kimi_search.py`：它是 Moonshot 直连 API，不是
  kimi-webbridge daemon；daemon 适配器按外部契约实现。
- 图片搜索、媒体下载、`pyimagedl` / `yt-dlp` 等不在当前范围。

### 复用时的硬约束

- 不引入 import 时副作用：`.env`、`logging.basicConfig`、全局 SQLite 状态、
  模块级 session、自动创建日志目录。
- 重依赖（`lxml`、`curl_cffi`、`firecrawl-py`、Node/Defuddle）必须懒加载，
  `core/*` 保持仅依赖 `pyyaml` + `jsonschema`。
- 复用文件保留上游版权；新增/修改记录在 `THIRD_PARTY.md`。

---

## 13. 迁移计划

### M0 — 契约层（无行为变化）

**状态：已完成（2026-10-25）**。`python -m pytest -q` 59 项通过；
`platforms` 与 `explain` 已可用。本文档 M0 交付物全部落地。

**交付物**

- `core/schema/platform.schema.json`、`tool.schema.json`、`routing.schema.json`
- `core/registry.py`：目录扫描、YAML 加载、Schema 校验、依赖检查
- `core/router.py`：优先级解析、`first_success` / `aggregate`
- `core/policy.py`：超时、输出上限、URL 安全、错误分类
- `adapters/base.py`：`Adapter` / `ToolCall` / `RawResult`
- `platforms/noop/`：最小示例平台
- 契约测试：schema、registry、router

**验收**

- `omnihub platforms` 能列出 `noop` 及其动作/优先级。
- 删除 `noop` 目录后命令仍正常，列表不再包含它。
- 现有 Agent-Reach 测试中与 dispatch/envelope 相关的契约全部通过。

**验收结果**

- `python -m omnihub.cli platforms` 输出 `noop`（search/read）。
- 删除目录的下线能力由 `tests/test_registry.py::test_deleting_directory_removes_platform`
  覆盖；manifest 错误、目录名不匹配、依赖缺失也会被隔离并报告。
- envelope 由 `tests/test_schema.py` 按 JSON Schema 校验。

### M1 — 适配器与管道

**状态：已完成（2026-10-25）**。`python -m pytest -q` 119 项通过；
`search` / `read` 已可用，并用真实 Exa/Jina 做了验收。

**交付物**

- `core/context.py`：有界子进程、HTTP、secret
- `adapters/http.py`、`cli.py`、`opencli.py`、`builtin.py`
- `core/pipeline.py`：search → parse → read、来源降级、attempts、聚合
- `core/normalize.py`、`core/template.py`
- `parse/generic.py`：JSON/YAML/OpenCLI/text + 平台自定义 `parse.py`
- `platforms/web/`：Exa search + Jina/Exa read
- `platforms/noop/`：离线 Python hooks，用于端到端测试

HTML/regex 通用解析器暂缓：当前不引入 `lxml`/`bs4`，站点 HTML 交给平台
自己的 `parse.py`；通用链对这两个名字会给出明确错误。

**验收**

- `omnihub web search` 与 `read` 完全走 manifest。
- 错误码、超时、`meta.attempts` 与旧实现一致。
- OpenCLI 的 YAML 错误块能被正确识别并映射。

**验收结果**

- `python -m omnihub.cli search "yt-dlp github repository" --platform web --limit 2`
  返回 Exa 结果，`backend: exa`。
- `python -m omnihub.cli read https://example.com --platform web`
  返回 Jina 正文，`backend: jina`。
- `tests/test_web_platform.py` 覆盖 Exa SSE、Jina header、antibot、Jina→Exa
  回退；`tests/test_adapters.py` 覆盖 OpenCLI YAML 错误映射。
- `tests/test_pipeline.py` 覆盖 source/平台降级、终态错误、聚合去重、
  `per_platform_limit` 和每平台默认值。

### M2 — 平台批次一

**状态：已完成（2026-10-25）**。`python -m pytest -q` 161 项通过；
`github`、`v2ex`、`bilibili` 的 `search/read` 已用真实网络验收，`doctor`
可报告适配器、可选凭据与登录提示。

**交付物**

- `platforms/github/`、`v2ex/`、`bilibili/`
- 每个平台：`platform.yaml` + fixtures + 解析测试 + 契约测试
- 登录态/公共网页兜底验证
- `doctor` 支持 `auth.secret`（可选凭据）且不泄露值

| 平台 | search | read | 可选登录 |
|---|---|---|---|
| `github` (100) | `GET /search/repositories`（token / 匿名两个来源） | `read.py`：仓库 + README、issue/PR + 前 50 条评论 | `GITHUB_TOKEN` / `GH_TOKEN` |
| `v2ex` (120) | SOV2EX `/api/search`；失败回退 Exa `site:v2ex.com` | `read.py`：`topics/show.json` + `replies/show.json` | 无 |
| `bilibili` (110) | Exa `site:bilibili.com` | `daemon browse` 解析 `cid` + 字幕签名地址 → `parse.py` 下载字幕 JSON | 浏览器登录态（daemon） |

路由默认值调整为 `web: 10`、`github: 100`、`bilibili: 110`、`v2ex: 120`：
普通搜索走公共网页，站点平台通过 `--platform` 或用户配置的 intent 使用；
`read` 仍按 URL 域名优先选站点平台，并把 `web` 作为最后兜底。

**验收结果**

- `search flask --platform github --limit 2` 返回 `pallets/flask`、
  `miguelgrinberg/flasky`，`backend: github-api`；配置 token 后来源变为
  `github-token`，响应与 attempts 中不出现 token。
- `read https://github.com/pallets/flask` 返回仓库描述 + README；
  `read https://github.com/pallets/flask/issues/42` 返回 issue 与评论。
- `search "python 限流" --platform v2ex` 返回 SOV2EX 命中；
  `read https://www.v2ex.com/t/1` 返回主题正文与回复。
- `search "python 教程" --platform bilibili` 返回 Exa 站点结果；
  `read https://www.bilibili.com/video/BV1S1dPBYEG9` 通过 kimi-webbridge
  在已登录浏览器里解析 `cid` 与字幕签名地址并下载字幕 JSON；daemon 或
  登录态缺失时来源记为 `SKIP`。
- `tests/test_platform_contracts.py` 覆盖优先级、域名路由、可选来源
  跳过/启用、凭据不泄露、doctor 与删除平台目录的隔离性。
- `Bilibili` 匿名 API 仍受 412 风控；字幕读取依赖浏览器登录态（daemon），
  字幕 JSON 本身用 `Referer` 直接从 `aisubtitle.hdslb.com` 下载。
- `player/v2` 的应答来自缓存池，每次返回的轨道切片不同，还会混入其他视频的
  AI 轨道；浏览器侧并发采样 10 次并按 `id_str` 去重，`parse.py` 再按字幕覆盖
  时长（`to / duration`）筛出真正属于本视频的轨道，人工轨道优先于 AI 轨道。

### M3 — 有状态工具与按需抓取

**状态：已完成（2026-10-25）**。`python -m pytest -q` 182 项通过；
kimi-webbridge daemon 适配器、`--enrich N` 与 `web` 浏览器读取来源均已
用真实网络/真实 daemon 验收。

**交付物**

- `adapters/daemon.py`：kimi-webbridge（`/status` 健康检查、session、
  cursor 分页、错误码映射）
- `core/context.py::loopback_http`：唯一允许 loopback 的传输入口，只接受
  `127.0.0.1` / `::1` / `localhost`，无凭据、无重定向
- `core/enrich.py` + `search --enrich N`：并发读取、失败隔离、URL 复检
- `parse/daemon.py`：把浏览器读取结果转成统一 item
- `platforms/web` read 来源链改为 `jina → kimi-browser → exa`，kimi 用
  `when: adapter:daemon` 保护；缺失时记为 `SKIP`
- source `when` 新增 `adapter:NAME`（适配器健康探针通过才运行）
- `doctor` 区分必需/可选适配器；只在 `when` 后出现的适配器不会把平台标成
  error

**实现要点**

- 地址来自 `KIMI_WEBBRIDGE_ADDR`、`~/.kimi-webbridge/daemon.addr` 或
  `config.json`，默认 `127.0.0.1:10086`；非 loopback 地址被拒绝。
- `read` 组合动作：`navigate` 后按 `aria` 快照读取，跟随 `next_cursor`
  直到字符预算、页数上限或 cursor 过期；过期时返回
  `truncated_reason: cursor_expired`，不把整次读取判为失败。
- `extension_not_connected` → `NEED_SETUP`；`busy` → `BUSY`；
  `timeout` → `TIMEOUT`；未配置扩展时 `web` 继续回退 Exa。
- `--enrich` 只处理 `text` 为空或短于 200 字符的结果，默认并发 3；失败
  写入 `meta.enrich_errors`，搜索结果仍然返回。

**验收结果**

- `doctor --text` 报告 `web` 的 `daemon` 适配器为 ok（真实 daemon
  `v2.0.23`，扩展已连接），其余平台无 error/degraded。
- `DaemonAdapter` 真实读取 `https://example.com/`：标题 `Example Domain`，
  983 字符；`max_chars=300` 时按预算截断并标记 `max_chars`。
- `search "example domain" --enrich 1` 返回 2 条结果，其中 1 条被补正文，
  `meta.enriched = 1`、`meta.enrich_errors = []`。
- 单元测试覆盖：daemon 未启动、扩展未连接、非法/远程地址、cursor 过期、
  预算截断、URL 复检、单条 enrich 失败隔离。

### M4 — 全量迁移与插件

**状态：插件部分与平台批次二已完成（2026-10-25），`python -m pytest -q`
202 项通过**；登录类平台迁移在 M5 批次三完成（见下文）。

**已完成**

- 插件发现：`$OMNIHUB_PLUGIN_PATH`、`~/.omnihub/plugins/`、
  `./.omnihub/plugins/`，同名覆盖内置；删除目录即下线。
- `plugin validate [PATH...]` / `plugin list`：只校验不执行；检查 schema、
  目录名与 id、跨文件引用、Python hooks 与能力声明，输出 errors/warnings。
- 插件信任：纯清单插件直接加载；带 `search.py` / `read.py` / `parse.py` / `transcribe.py`
  的插件必须声明 `capabilities`（`http` / `exec` / `browser`，纯计算写
  `[]`），并在 `--trust-plugin <id>` 或 `OMNIHUB_TRUSTED_PLUGINS` 后才执行。
  未信任时平台仍可被发现，运行时返回 `NEED_SETUP`。
- `explain` 增加 `platforms[]`：按计划顺序列出平台来源的 adapter、name、
  priority、`when`、`parse`、`disabled`；`--text` 同步展示。
- `skill [--output FILE]`：从 registry 生成 SKILL.md（命令、平台表、每个
  平台的参数与来源顺序），不包含凭据与来源 `args`。

**平台批次二（2026-10-25）**

- `platforms/reddit/`：Exa `site:reddit.com` 搜索，Exa fetch 读取帖子与
  高赞评论，daemon 可用时用真实浏览器兜底。实测 Reddit 匿名 JSON 与 Jina
  都返回 403，因此不引入这两个来源。
- `platforms/youtube/`：Exa `site:youtube.com` 搜索，Exa fetch 读取视频页
  正文/字幕文本（实测可拿到完整字幕），daemon 为兜底来源。
- 两个平台均为纯 manifest；共享 `exa_search` / `exa_read` 解析器。补充
  `exa_read` 对“`# 标题` + URL/Published/Author”页面头的解析（搜索结果的
  `Title:` 块格式保持不变）。

**待完成**

- facebook、instagram、boss 按当前范围明确不迁移（强风控或缺少可用来源）。
  其余登录态平台已在 M5 批次三完成。
- 删除静态 registry 与旧 channel 类（本仓库已无旧 channel 类，此条只对
  上游迁移过程有意义）。

**验收结果**

- 第三方声明式平台不改核心即可加入；带代码插件显式信任后端到端可运行。
- `platforms` / `skill` 的能力表随目录增删自动变化。
- 测试覆盖：hooks 缺能力声明被拒、环境变量/参数信任、未信任时拒绝执行、
  `plugin validate` 的 errors/warnings/缺失路径、explain 来源、skill 输出
  不含 secret。
- 真实网络：Reddit 搜索/读取、YouTube 搜索/读取均返回 `ok:true`；
  `doctor --text` 中 reddit/youtube 为 `ok`，daemon 为可选适配器。

**M5 平台 live 回归（2026-10-25）**

- manifest 新增顶层 `check.search` / `check.read`：值就是该 action 的参数
  对象（如 `{query, limit}`、`{target}`），由 schema 校验。检查随平台目录
  增删自动出现或消失，核心不需要维护平台清单。
- `check [--platform ID]... [--action search|read] [--routing FILE]
  [--timeout S] [--dry-run] [--text]`：按平台优先级遍历，经正常 pipeline
  以显式平台路由执行，输出每项 `status` / `backend` / `items` /
  `elapsed_ms` / `error`，`meta` 含 `count` / `failed` / `skipped` /
  `dry_run`。失败时 `ok:false`、错误码 `UPSTREAM_BROKEN`、退出 1；未知
  平台 `BAD_INPUT`、退出 2。
- `--dry-run` 只列计划，不创建 `Context`、不联网。未声明 `check` 的平台
  （如离线示例 `noop`）计入 `meta.skipped`。
- `web`、`github`、`v2ex`、`bilibili`、`reddit`、`youtube` 均声明了
  `search` 与 `read` 检查；`check` 的参数名会与 action 声明的参数做一致性
  测试。
- **验收结果**：`python -m pytest -q` 211 项通过（离线）；真实网络
  `check` 的 12 项全部 `ok`——github `github-api`，web `exa`/`jina`，
  v2ex `sov2ex`/`v2ex-api`，bilibili `exa`/`kimi-browser`，reddit 与
  youtube 均为 `exa`。

**M5 统一限流与冷却（2026-10-25）**

- `core/ratelimit.py`：跨进程账本 `~/.omnihub/state/rate_limits.json`
  （`OMNIHUB_STATE_DIR` 可改路径；目录 0700、文件 0600，原子替换，POSIX 下
  `flock` 串行化读改写）。只记录平台/来源名、错误码与时间戳，不记录输入、
  URL 或凭据；损坏或不可写时 fail open。
- 两级暂停：`RATE_LIMITED` / `PLATFORM_BLOCKED` 冷却整个平台（默认 300s /
  900s，优先采用 `Retry-After`，上限 6h）；`TIMEOUT` / `UPSTREAM_BROKEN` /
  `NEED_SETUP` 只暂停单个来源（60 / 120 / 60s）。来源暂停不阻止平台回退，
  平台冷却与终止码一致。
- pipeline 在调用适配器前检查暂停：命中时写 `result: SKIP` 的 attempt，
  带 `reason` 与 `retry_after_seconds`，不发起网络/进程调用；成功调用清除
  该来源暂停。最终错误的 `error.retry_after_seconds` 暴露剩余秒数
  （`OmniHubError` 新增可选字段，`envelope` 仅在存在时输出，schema_version
  仍为 1）。
- `Context` 的 HTTP 429/403 解析 `Retry-After`（秒数或 HTTP 日期）。
- source 可声明 `min_interval`（秒），核心 `clamp_min_interval` 限制在 10s
  以内；重复调用同一来源时只等待剩余间隔。
- **验收结果**：`python -m pytest -q` 235 项通过；覆盖默认/自定义冷却、
  短冷却不缩短旧冷却、来源暂停隔离、平台回退、pacing、损坏文件、0600/0700
  权限与 symlink 拒写。

**M5 适配器边界测试（2026-10-25）**

- fake binary：在临时目录写可执行脚本，经 CLI 适配器验证 stdout JSON 解析、
  返回码记录与登录墙错误映射。
- 本地 fake HTTP server：`ThreadingHTTPServer` 随机端口，验证
  `loopback_http` 的真实 socket 路径、JSON 解析与 content type。
- `Context` 的 429/403 覆盖 `Retry-After` 秒数与 HTTP 日期两种形式，
  并验证缺失时 `retry_after_seconds` 为 `None`。
- fixture 覆盖：`github`、`v2ex`、`bilibili`、`reddit`、`youtube` 均有
  `search` / `read` 真实响应样本与契约用例（补齐 bilibili / youtube 的
  Exa 搜索样本）。
- **验收结果**：`python -m pytest -q` 243 项通过。

**M5 冷却可见性与重置（2026-10-25）**

- `RateLimits.active()` 列出未过期的平台冷却与来源暂停（scope、platform、
  source、code、剩余秒数），按剩余时间倒序；`RateLimits.clear(platform)`
  删除匹配条目，可只清一个平台，返回清除数量；pacing 时间戳不受影响。
- `doctor` 接受可选 `rate_limits`：每个平台行新增 `cooldowns`，`meta` 新增
  `cooling_count`，`--text` 输出 `cooling <scope>`；冷却属于临时状态，不改变
  平台 `status` 或 `ok`。
- 新命令 `cooldowns [list|reset] [--platform ID]...`：默认列出账本；`reset`
  清除全部或指定平台，输出 `meta.cleared` 与清除后仍生效的条目；未知平台
  以 `BAD_INPUT` 退出 2。重置是显式操作，不会自动绕过限流。
- `check` 区分“未运行”和“失败”：所有 attempt 都是账本 `SKIP` 且错误带
  `retry_after_seconds` 时，检查记为 `status: cooling`、`ok: null`、附
  `retry_after_seconds`，不计入 `failed`，`meta.cooling` 单独统计；来自网络
  的真实 429 仍有 `RUN` attempt，保持 `failed`。
- **验收结果**：`python -m pytest -q` 256 项通过；覆盖 active 排序与过期
  过滤、按平台/全部清除、pacing 保留、空账本不落盘、CLI list/reset 与未知
  平台、doctor 冷却展示、check 的 cooling/failed 区分。

**M5 DNS 解析加固（2026-10-25）**

- `policy.ensure_public_resolution(url)` 在发起 HTTP 前解析主机名，要求全部
  结果为 `ipaddress.is_global`；`Context.http` 对初始 URL 与每次重定向都复检。
  阻止公网域名指向 loopback/私网（如 `localtest.me`）的 SSRF；字面 IP 不再
  解析，DNS 失败映射 `UPSTREAM_BROKEN`，非公网答案映射 `BAD_INPUT`。
- 残余风险：requests 连接时会再次解析，DNS rebinding 仍有窄窗口；当前方案
  覆盖常见场景，不做连接级 IP pinning。
- **验收结果**：`python -m pytest -q` 262 项通过；新增 loopback/混合答案/
  DNS 失败/重定向/字面 IP 用例；真实 `check --platform web` 仍为 ok。

**M5 登录态平台批次三（2026-10-25）**

- `daemon` 适配器新增 `browse` 复合动作：`navigate` → 读取真实
  `location.href`/`document.title` → 登录 URL 标记检查 → 轮询 `wait_for`
  选择器（上限 `wait_ms`，最大 30s）→ 在同一页面执行站点 JavaScript →
  `JSON.stringify` 结果解码 → 按 `limit` 截断。
- 站点 JavaScript 只存在于平台目录的 manifest 中，核心适配器不知道任何
  站点选择器。搜索词通过 `location` / `URLSearchParams` 从已加载页面读取，
  manifest 不做字符串插值，避免把用户输入拼进 JS 源码。
- 结构化标记：`{login:true}` → `NEED_LOGIN`，`{blocked:true}` →
  `PLATFORM_BLOCKED`，`{not_found:true}` → `NOT_FOUND`；其余站点错误继续
  回退到下一来源。
- 安全：`browse` 会在用户已登录的真实浏览器里执行任意 JS，因此只允许
  builtin 或显式信任的平台调用；未信任平台返回 `NEED_SETUP`。
- SPA 的 `load` 事件经常不触发。`navigate` 超时后不再直接失败：先用
  `list_tabs` 复用当前 session 标签，否则从 `list_browser_tabs` 里按
  `group_title`、目标 URL、同 host 且属于 `omnihub:` 分组的顺序恢复并
  `attach_tab`；恢复失败才返回原始超时。
- 新平台（均为 `level: optional`，`capabilities: [browser]`；daemon 来源
  用 `when: adapter:daemon` 保护，daemon 缺失时平台在 `doctor` 中为
  `degraded` 而不是 `error`）：
  - `twitter` (150)：X 搜索页 `f=live`，提取 `article`、
    `[data-testid="tweetText"]`、`User-Name`、`time[datetime]`；推文详情读
    第一条 `article`。Exa `site:x.com` 作为搜索兜底。
  - `xueqiu` (160)：`/k?q=` 搜索页提取 `.timeline__item`、标题/正文/作者/
    时间；状态页读取 `h1` 与 `.article__bd__detail` / `.status-content`。
    雪球页面不触发 load，单次导航固定约 30s，仍在 60s 来源预算内。
  - `xiaohongshu` (170)：`search_result` 页提取 `section.note-item`、
    `data-note-id`、`a.title`、`a.author`；详情读 `#detail-title`、
    `#detail-desc` / `.note-content`。读链接必须带搜索结果的 `xsec_token`，
    因此该平台只声明 `check.search`。
  - `xiaoyuzhou` (180)：站点没有可用的内容搜索路由，搜索走 Exa
    `site:xiaoyuzhoufm.com`；读取为 Jina → daemon → Exa。`transcribe` 从单集
    页面的 `__NEXT_DATA__` 提取公开音频地址，拒绝付费/私密单集，经 ffmpeg
    压缩为 16kHz 单声道并按 10 分钟切片后调用 Groq 或 OpenAI 的 Whisper
    兼容接口；上传前检查 25MB/切片数/总字节上限，密钥只从 `ctx.secret`
    读取。`--polish` 使用 Groq Llama 补中文标点，失败时回退原始转写。
  - `linkedin` (190)：在已登录页面内调用 public guest API
    `/jobs-guest/jobs/api/seeMoreJobPostings/search` 与
    `/jobs-guest/jobs/api/jobPosting/<id>`，用正则解析 HTML（LinkedIn 的
    Trusted Types 阻止 `DOMParser`/`innerHTML`）。HTTP 非 2xx →
    `{blocked:true}`，404 → `{not_found:true}`。
- facebook、instagram、boss 不在此批次（按当前范围明确不迁移）。
- **验收结果**：`python -m pytest` 278 项通过；新增 18 项 daemon/browse
  单测与 8 项平台契约测试；真实 `check` 21 项全部 `ok`（twitter / xueqiu /
  xiaohongshu / linkedin 为 `kimi-browser`，xiaoyuzhou 为 `exa` / `jina`），
  耗时约 2 分钟，其中雪球两次导航各约 30s。

**M5 浏览器标签页清理（2026-10-25）**

- `read` / `browse` 每次调用生成独立 session（`omnihub-<platform>-<随机后缀>`），
  `finally` 中调用 `close_session`，成功、业务错误、超时都会关闭本次查询的
  全部标签页。独立 session 让并发的 `search --enrich` 读取不会互相关闭对方
  的标签。
- 清理是 best effort：`close_session` 失败被吞掉，不能覆盖动作本身的错误。
  清理使用独立的 5s 超时，不复用动作可能很长的超时。
- 超时恢复只接管 `omnihub:` 分组内的标签（`tagged` / `same_page_owned` /
  `same_host_owned`）。此前 `same_page` 可能匹配 URL 相同的用户标签；自动
  关闭后这会误关用户页面，因此收紧为仅限自己的分组。
- **验收结果**：`python -m pytest` 333 项通过；真实 `check --platform
  linkedin --action search` 后 `list_browser_tabs` 中 `omnihub:` 标签为 0。

**M5 工具型平台补充（2026-10-25）**

- `deepwiki` (200)：builtin hook 直接 POST 公开 MCP 端点
  `https://mcp.deepwiki.com/mcp`，调用 `read_wiki_contents`，兼容 JSON 与
  SSE（`data:` 行）响应。目标归一支持 `owner/repo`、`github.com` 仓库 URL
  与 `deepwiki.com` URL；未索引仓库映射 `NOT_FOUND` 并提示先索引。MCP 没有
  搜索工具，因此只声明 `read`，也不需要 Node 或凭据。
- `notebooklm` (210)：builtin hook 调用本机 `notebooklm` CLI。
  `search` 用 `notebooklm list --json` 拉全量后按标题/id 本地过滤（CLI 没有
  服务端过滤）；`read` 用 `notebooklm summary --json --topics`，先把
  notebook URL 归一为 id，返回摘要与建议问题。
- `notebooklm` 是可选工具：来源带 `when: tool:notebooklm`，缺失时平台在
  `doctor` 中为 `degraded`。`check` 对“所有来源均 SKIP”的检查记录为
  `skipped`（`ok: null`）而不是 `failed`，避免可选工具缺失被当成上游回归。
- **验收结果**：`python -m pytest` 325 项通过；`doctor` 14 个平台、0 error、
  1 degraded（缺 notebooklm CLI）；真实 `check` 23 项、0 failed、2 skipped
  （noop 无 check；notebooklm 跳过），其中 `deepwiki` 经 `deepwiki-mcp`
  返回 ok，耗时约 1.9s。

### M5 — 加固与发布

**交付物**

- 每个平台的 fixture 解析测试
- 适配器单测（本地 fake binary / fake HTTP server）
- `omnihub check` live 回归
- 文档、迁移指南、发布验收

**验收**

- 无网络环境下测试全绿。
- live check 覆盖 `search/read` 主路径。
- 回归对比旧版：结果字段、错误码、退出码一致。

---

## 14. 测试策略

| 层级 | 内容 |
|---|---|
| Schema | 每个 `platform.yaml` / `tool.yaml` / `routing.yaml` 通过 JSON Schema |
| 平台契约 | 目录存在、三项能力可解析、actions/params 合法、依赖可满足 |
| 解析器 | fixture 输入 → 期望 Item；每个来源至少一个样本 |
| 适配器 | fake 命令 / 本地 HTTP server；超时、非零退出、超大输出、YAML 错误 |
| 浏览器复合动作 | fake daemon：选择器轮询、JSON 解码、登录/风控/未找到标记、导航超时后的标签恢复、未信任平台拒绝 |
| 路由 | 显式平台、intent、优先级、禁用平台、聚合去重 |
| 限流 | 注入时钟/路径；冷却合并、来源隔离、平台回退、pacing、active/clear、损坏文件与权限 |
| 删除 | 删除平台目录后 registry 仍可加载 |
| live | 显式开启；`omnihub check --platform <id>`（`--dry-run` 离线列出计划） |

测试隔离沿用现有做法：临时 HOME、禁用真实网络与 OpenCLI 调用。

---

## 15. 风险与缓解

| 风险 | 缓解 |
|---|---|
| YAML 过度抽象，复杂平台写不出来 | 声明式优先 + 平台目录内 Python 扩展 |
| 解析器随站点改版失效 | fixture 测试 + live check + 失败降级 |
| 各平台重复实现工具调用 | 共享 adapters；平台只声明 `adapter/tool/args` |
| 有状态工具（kimi/opencli）与一次性模型不匹配 | `daemon` 适配器支持 session/cursor；核心管道保持无状态 |
| 子进程输出无上限 | 给 `run_cmd` 增加 `max_output_bytes` |
| 插件执行任意代码 | 纯清单默认安全；代码插件显式 `--trust-plugin` + 能力声明 |
| 聚合模式放大限流/反爬 | 默认 `first_success`；聚合限制 top K 与并发 |
| 迁移期间两套模型并存 | `LegacyChannelAdapter` + 平台级开关，逐个切换 |

---

## 16. 待确认事项

1. 落地仓库最终名称：`services/omnihub/` 还是其他。
2. 是否直接复制 Agent-Reach 代码作为起点（保留 MIT 声明），还是 clean-room 只借鉴架构。
3. 第三方插件是否允许发布为 pip 包（entry points），首版是否只支持本地目录。
4. 平台包是否需要独立版本号（`platform.yaml: version`）。
5. `web` 是否作为不可删除的核心兜底。

---

## 17. 当前代码映射（迁移参考）

| 现有位置 | 迁移后 |
|---|---|
| `channels/<platform>.py` | `platforms/<id>/platform.yaml` + 可选 py |
| `channels/base.py:30-49` `Action.backends` | `platform.yaml` 的 `sources[].priority` |
| `channels/__init__.py:24-39` 静态列表 | `core/registry.py` 目录扫描 |
| `runtime/dispatch.py:200` `run_action` | `core/pipeline.py` + `core/router.py` |
| `runtime/tools.py:52-68` `TOOLS` | `omnihub/tools/*.yaml` + `adapters/*` |
| `runtime/manage.py:954` `CONFIG_KEYS` | `core/config.py` 分层配置 |
| `runtime/public_web.py:56-64` | `platforms/web/` 的公共来源 + 平台 `public_web.scopes` |
| `backends/opencli.py:59` | `adapters/opencli.py` |
| `channels/_opencli_site.py:11` | 平台 manifest 的 `adapter: opencli` 声明 |
| `runtime/run.py:30` `run_cmd` | `adapters/cli.py` + `policy.py` 输出上限 |
| `runtime/run.py:111` `http_request` | `adapters/http.py` |
| `runtime/result.py:85,116` | `core/result.py`（原样保留） |
| `config.py:155` 扁平配置 | `core/config.py` 分层配置 |
| `~/.claude/skills/kimi-webbridge/SKILL.md` | `adapters/daemon.py` |

---

## 18. 下一步

M0–M4 已全部落地；M5 的 `check` live 回归、统一限流/冷却、冷却可见性与
重置、适配器边界测试、迁移指南与发布验收也已完成。剩余：

- 登录/浏览器依赖平台中的 twitter、xueqiu、xiaohongshu、xiaoyuzhou、
  linkedin 已通过 daemon `browse` 迁移（批次三）；facebook、instagram、boss
  明确不迁移。来源确认结论保留在 `docs/migration.md` 第 5 节。
- 已声明但未实现的 `mcp` / `browser` 适配器。当前浏览器能力由 `daemon`
  的 `browse` 复合动作提供，不需要单独的 `browser` 适配器。
