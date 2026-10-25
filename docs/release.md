# 0.1.0 发布验收

- **日期**：2026-10-25
- **版本**：`0.1.0`（`pyproject.toml`）
- **范围**：M0 契约层、M1 执行层与 `web`、M2 平台批次一、M3 daemon 与
  `--enrich`、M4 插件信任 / `explain` / `skill` / 平台批次二、M5 `check`
  与统一限流冷却、登录态平台批次三（daemon `browse`）。
- **环境**：Linux 6.8，Python 3.12.3；真实网络可用。

## 1. 离线验收

```bash
python -m pytest -q
# 278 tests
```

覆盖：schema 与 manifest 契约、目录删除隔离、路由与聚合、模板与解析器、
HTTP/CLI/OpenCLI/daemon/builtin 适配器、daemon `browse`（选择器等待、JSON
解码、登录/风控/未找到标记、导航超时后的标签恢复）、`check` 聚合与
cooling/failed 区分、插件信任、冷却账本（查询 / 清除 / pacing）、
`Retry-After` 解析、DNS 解析校验、真实 loopback HTTP server 与 fake binary。

安全相关用例：

| 项 | 结果 |
|---|---|
| 私有/loopback/带 userinfo 的 URL 被拒 | 通过 |
| 公网域名解析到私网地址时请求被拒（含重定向） | 通过 |
| 跨域重定向移除 `Authorization` / `Cookie` | 通过 |
| CLI 使用 argv 数组、禁止 `shell=True` | 通过 |
| 超时杀进程组、输出截断且排空管道 | 通过 |
| daemon 仅允许 loopback、地址来自本地配置 | 通过 |
| 未信任插件执行 Python 返回 `NEED_SETUP` | 通过 |
| 未信任平台调用 `browse`（浏览器内任意 JS）返回 `NEED_SETUP` | 通过 |
| `browse` 目标 URL 复检私有地址（浏览器内重定向不在本层复检） | 通过 |
| 冷却账本目录 0700、文件 0600、拒绝 symlink 写入 | 通过 |

## 2. 静态体检

```bash
python -m agent_reach.cli doctor --text
# 14 platform(s), 0 error(s), 1 degraded
```

| 平台 | 动作 | 必需适配器 | 可选凭据 / 浏览器 |
|---|---|---|---|
| `web` | search, read | http | daemon 可选 |
| `github` | search, read | builtin, http | `GITHUB_TOKEN`（缺省用公共来源） |
| `bilibili` | search, read | builtin, http | `BILIBILI_COOKIE`（缺省用 Jina/Exa） |
| `v2ex` | search, read | builtin, http | — |
| `reddit` | search, read | http | daemon 可选 |
| `youtube` | search, read | http | daemon 可选 |
| `twitter` | search, read | http | daemon 可选（Exa 搜索兜底） |
| `xueqiu` | search, read | — | daemon 可选 |
| `xiaohongshu` | search, read | — | daemon 可选 |
| `xiaoyuzhou` | search, read | http | daemon 可选 |
| `linkedin` | search, read | — | daemon 可选 |
| `deepwiki` | read | builtin | —（公开 MCP，无凭据） |
| `notebooklm` | search, read | builtin | `notebooklm` CLI 可选（缺失时 degraded） |
| `noop` | search, read | builtin | —（离线示例） |

`kimi-webbridge v2.0.23` 在本机运行。`twitter` / `xueqiu` / `xiaohongshu` /
`linkedin` 的搜索与读取依赖它；daemon 未运行时这些平台在 `doctor` 中为
`degraded`（来源被 `when: adapter:daemon` 跳过），不会把整体健康检查判为
失败。`notebooklm` 依赖本机的 `notebooklm` CLI（`pip install notebooklm-py`）；
本机未安装，因此 `doctor` 显示 `degraded`，来源被 `when: tool:notebooklm`
跳过。`deepwiki` 只走公开 HTTP MCP 端点，不需要登录或凭据。

## 3. 真实网络验收

```bash
python -m agent_reach.cli check --timeout 90 --text
# 23 check(s), 0 failed, 2 skipped
```

| 平台 | 动作 | 实际 backend | 结果 |
|---|---|---|---|
| web | search | exa | ok |
| web | read | jina | ok |
| github | search | github-api | ok |
| github | read | github-api | ok |
| bilibili | search | exa | ok |
| bilibili | read | jina | ok |
| v2ex | search | sov2ex | ok |
| v2ex | read | v2ex-api | ok |
| reddit | search | exa | ok |
| reddit | read | exa | ok |
| youtube | search | exa | ok |
| youtube | read | exa | ok |
| twitter | search | kimi-browser | ok |
| twitter | read | kimi-browser | ok |
| xueqiu | search | kimi-browser | ok |
| xueqiu | read | kimi-browser | ok |
| xiaohongshu | search | kimi-browser | ok |
| xiaoyuzhou | search | exa | ok |
| xiaoyuzhou | read | jina | ok |
| linkedin | search | kimi-browser | ok |
| linkedin | read | kimi-browser | ok |
| deepwiki | read | deepwiki-mcp | ok |
| notebooklm | search | — | skipped（本机未安装 CLI） |

`noop` 不声明 `check`，`notebooklm` 的工具缺失导致来源全部 `SKIP`，二者均
计入 `skipped` 而不是 `failed`。多数检查耗时 0.1–3s；雪球页面不触发
`load`，导航固定等待约 30s，因此 `xueqiu` 两项各约 30s。

## 4. 功能验收

- **平台可删除**：删除任一平台目录后 registry 仍加载，其余平台与
  `skill` / `check` 列表同步变化（测试覆盖）。
- **两级优先级与路由**：`--platform` > URL 域名 > intent > 平台 priority >
  id；域名路由到 `github`/`reddit`/`youtube` 等站点平台先于 `web`。
- **降级与终止码**：非终止错误继续尝试下一来源/平台；`RATE_LIMITED`、
  `PLATFORM_BLOCKED`、`NEED_LOGIN`、`NOT_FOUND`、`BAD_INPUT`、`BUSY` 停止。
- **冷却**：平台级 300s/900s（`Retry-After` 优先，上限 6h），来源级
  60/120/60s；命中时 attempt 为 `SKIP` 并带 `retry_after_seconds`。
  `doctor` 将未过期条目显示为 `cooling`，`cooldowns` 可列出或显式重置；
  `check` 对账本跳过的项报告 `cooling` 而不是失败。
- **插件**：声明式插件直接加载；带 Python hooks 的插件必须声明
  `capabilities` 并显式信任。
- **输出**：`schema_version: 1` envelope；`error.retry_after_seconds` 仅在
  已知时出现；退出码 0 / 1 / 2。

## 5. 已知限制

- facebook、instagram、boss 明确不在当前范围，未提供平台目录。
- `browse` 会在用户的真实浏览器里执行平台 manifest 提供的 JavaScript，
  因此只对 builtin 或显式信任的平台开放；第三方平台即使被加载也不能调用。
- 小红书的读取链接必须带搜索结果里的 `xsec_token`，token 会过期，所以
  `check` 只覆盖搜索；读取需要先用搜索拿到新链接。
- 雪球页面不触发浏览器 `load` 事件，每次 `navigate` 约等待 30s；仍在
  来源 60s 预算内，但 `check` 整体耗时会明显增加。
- `mcp` 与 `browser` 适配器已声明但未实现；manifest 引用会得到
  `NEED_SETUP`。浏览器能力当前由 `daemon` 的 `browse` 提供。
- 平台级冷却会暂停该平台的全部来源。若限流来自共享托管 API（如 Exa），
  daemon 兜底来源也会一并暂停，直到冷却到期。这是为遵守“不要切后端”
  的终止码语义。
- `check` 走正常 pipeline，因此会尊重冷却账本。调试“刚失败过的平台”时，
  可以等待 `retry_after_seconds`，或确认上游恢复后运行
  `cooldowns reset --platform <id>`（等价于删除
  `~/.agent-reach/state/rate_limits.json` 中对应条目）。
- live 结果依赖 GitHub / Exa / Jina / SOV2EX / V2EX 的当前行为，上游变更
  需要更新对应平台的来源或解析器。
