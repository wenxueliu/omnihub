# reach-platforms

配置驱动、按平台组织的 Agent 互联网访问层。

当前完成 **M1**：M0 契约层（目录发现、manifest 校验、两级优先级路由）+
HTTP/CLI/OpenCLI 适配器、执行管道，以及 `web` 公共网页平台
（Exa 搜索、Jina/Exa 读取）。CLI 已支持 `platforms`、`explain`、`search`
和 `read`。daemon（kimi-webbridge）、MCP、浏览器适配器与平台批次迁移在
后续里程碑，详见 `docs/implementation.md`。

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

# 离线契约示例平台
python -m agent_reach.cli search hello --platform noop --limit 2 --text

# 安装后也可用 reach-platforms 或 agent-reach
pip install -e .
reach-platforms platforms --text
```

`web` 是公共网页兜底平台：搜索走 Exa hosted MCP（无需 Key），读取优先
Jina Reader，失败后回退 Exa。`read` 按 URL 域名先选站点平台，再自动把
`web` 作为最后一个来源。

## 目录

```
agent_reach/
  cli.py                 # platforms / explain / search / read
  core/
    config.py            # 分层 routing 配置
    context.py           # 子进程、HTTP、secret；超时/输出上限
    normalize.py         # item 归一、URL 规范化、聚合去重
    pipeline.py          # 路由 → 来源降级 → 解析 → envelope
    policy.py            # 超时、输出上限、URL 安全、错误分类
    registry.py          # 扫描/校验/索引 platform.yaml
    result.py            # Item / envelope / error codes（复用 Agent-Reach）
    router.py            # 显式平台 / URL / intent / priority
    schema/              # JSON Schema
    template.py          # ${param} / ${secret:NAME} 模板
  adapters/
    base.py              # Adapter / ToolCall / RawResult 协议
    builtin.py           # 平台目录内可信 Python hooks
    http.py / cli.py / opencli.py
  parse/generic.py       # JSON / YAML / OpenCLI / text 解析链
  platforms/noop/        # 最小离线平台（契约测试/示例）
  platforms/web/         # Exa + Jina 公共网页平台
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
