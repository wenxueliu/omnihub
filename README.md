# reach-platforms

配置驱动、按平台组织的 Agent 互联网访问层。

当前完成 **M2**：M0 契约层（目录发现、manifest 校验、两级优先级路由）+
M1 执行层（HTTP/CLI/OpenCLI 适配器、`web` 公共网页平台）+ M2 平台批次一
（`github`、`v2ex`、`bilibili`）。CLI 已支持 `platforms`、`doctor`、
`explain`、`search` 和 `read`。daemon（kimi-webbridge）、MCP、浏览器
适配器、`--enrich` 与后续平台批次详见 `docs/implementation.md`。

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

# 站点平台
python -m agent_reach.cli search "flask" --platform github --limit 3
python -m agent_reach.cli read https://github.com/pallets/flask/issues/42 --text
python -m agent_reach.cli search "python 限流" --platform v2ex --limit 3
python -m agent_reach.cli read https://www.v2ex.com/t/1 --text
python -m agent_reach.cli search "rust 教程" --platform bilibili --limit 3
python -m agent_reach.cli read https://www.bilibili.com/video/BV1rpWjevEip --text

# 检查工具、适配器、凭据与登录提示（不发起网络请求）
python -m agent_reach.cli doctor --text

# 离线契约示例平台
python -m agent_reach.cli search hello --platform noop --limit 2 --text

# 安装后也可用 reach-platforms 或 agent-reach
pip install -e .
reach-platforms platforms --text
```

`web` 是公共网页兜底平台：搜索走 Exa hosted MCP（无需 Key），读取优先
Jina Reader，失败后回退 Exa。它的默认优先级是 10，因此普通 `search`
默认走公共网页；站点平台（`github` 100、`bilibili` 110、`v2ex` 120）通过
`--platform`，或 routing 里自定义的 `intents` 使用。`read` 按 URL 域名先
选站点平台，再自动把 `web` 作为最后一个来源。

- `github`：REST 搜索仓库；读取仓库（含 README）、issue、PR。设置
  `GITHUB_TOKEN` 或 `GH_TOKEN` 可提高限额并访问私有仓库。
- `v2ex`：SOV2EX 搜索主题，V2EX 公共 API 读取主题和第一页回复。
- `bilibili`：Exa `site:bilibili.com` 搜索；读取优先 Jina，配置
  `BILIBILI_COOKIE` 后可用视频 API 获取结构化元数据。

## 目录

```
agent_reach/
  cli.py                 # platforms / doctor / explain / search / read
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
  adapters/
    base.py              # Adapter / ToolCall / RawResult 协议
    builtin.py           # 平台目录内可信 Python hooks
    http.py / cli.py / opencli.py
  parse/
    generic.py           # JSON / YAML / OpenCLI / text 解析链
    web.py               # 共享 Exa / Jina 解析器
  platforms/noop/        # 最小离线平台（契约测试/示例）
  platforms/web/         # Exa + Jina 公共网页平台
  platforms/github/      # GitHub 搜索、仓库/issue/PR 读取
  platforms/v2ex/        # SOV2EX 搜索、主题与回复读取
  platforms/bilibili/    # Exa 搜索、Jina / Cookie API 读取
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

可选来源用 `when: secret:NAME` 或 `when: tool:NAME` 声明：条件不满足时
记为 `SKIP`，不会让整个平台失败。平台的 `auth.secret` 表示“可选登录
凭据”，缺失时 `doctor` 提示但仍使用公共来源；`requires.secrets` 则表示
必需凭据。

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

安全策略（超时上限、输出上限、URL 白名单）不在 routing 配置里，平台不能
通过 YAML 放宽这些限制。凭据通过 `${secret:NAME}` 从环境变量或
`~/.agent-reach/secrets.yaml` 注入，不写进 manifest 输出。

## 设计文档

- [`docs/implementation.md`](docs/implementation.md) — 完整设计、契约、迁移计划
- [`THIRD_PARTY.md`](THIRD_PARTY.md) — 复用代码与许可

## License

MIT。复用的上游代码版权与许可见 `LICENSE` 和 `THIRD_PARTY.md`。
