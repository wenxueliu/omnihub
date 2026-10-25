# 0.1.0 发布验收

- **日期**：2026-10-25
- **版本**：`0.1.0`（`pyproject.toml`）
- **范围**：M0 契约层、M1 执行层与 `web`、M2 平台批次一、M3 daemon 与
  `--enrich`、M4 插件信任 / `explain` / `skill` / 平台批次二、M5 `check`
  与统一限流冷却。
- **环境**：Linux 6.8，Python 3.12.3；真实网络可用。

## 1. 离线验收

```bash
python -m pytest -q
# 256 tests
```

覆盖：schema 与 manifest 契约、目录删除隔离、路由与聚合、模板与解析器、
HTTP/CLI/OpenCLI/daemon/builtin 适配器、`check` 聚合与 cooling/failed 区分、
插件信任、冷却账本（查询 / 清除 / pacing）、`Retry-After` 解析、真实
loopback HTTP server 与 fake binary。

安全相关用例：

| 项 | 结果 |
|---|---|
| 私有/loopback/带 userinfo 的 URL 被拒 | 通过 |
| 跨域重定向移除 `Authorization` / `Cookie` | 通过 |
| CLI 使用 argv 数组、禁止 `shell=True` | 通过 |
| 超时杀进程组、输出截断且排空管道 | 通过 |
| daemon 仅允许 loopback、地址来自本地配置 | 通过 |
| 未信任插件执行 Python 返回 `NEED_SETUP` | 通过 |
| 冷却账本目录 0700、文件 0600、拒绝 symlink 写入 | 通过 |

## 2. 静态体检

```bash
python -m agent_reach.cli doctor --text
# 7 platform(s), 0 error(s), 0 degraded
```

| 平台 | 动作 | 必需适配器 | 可选凭据 |
|---|---|---|---|
| `web` | search, read | http | — |
| `github` | search, read | builtin, http | `GITHUB_TOKEN`（缺省用公共来源） |
| `bilibili` | search, read | builtin, http | `BILIBILI_COOKIE`（缺省用 Jina/Exa） |
| `v2ex` | search, read | builtin, http | — |
| `reddit` | search, read | http | — |
| `youtube` | search, read | http | — |
| `noop` | search | builtin | —（离线示例） |

`kimi-webbridge v2.0.23` 在本机运行，`web` / `reddit` / `youtube` 的
daemon 来源探测为 `ok`，但仍按可选来源处理。

## 3. 真实网络验收

```bash
python -m agent_reach.cli check --timeout 60 --text
# 12 check(s), 0 failed, 1 platform(s) without check
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

`noop` 不声明 `check`，计入 `skipped`。单次耗时约 0.1–3s，受第三方服务影响。

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

- 登录或浏览器依赖的平台（twitter、xueqiu、xiaohongshu、xiaoyuzhou、
  linkedin、facebook、instagram、boss）尚未迁移，需先确认可用来源。
- `mcp` 与 `browser` 适配器已声明但未实现；manifest 引用会得到
  `NEED_SETUP`。
- 平台级冷却会暂停该平台的全部来源。若限流来自共享托管 API（如 Exa），
  daemon 兜底来源也会一并暂停，直到冷却到期。这是为遵守“不要切后端”
  的终止码语义。
- `check` 走正常 pipeline，因此会尊重冷却账本。调试“刚失败过的平台”时，
  可以等待 `retry_after_seconds`，或确认上游恢复后运行
  `cooldowns reset --platform <id>`（等价于删除
  `~/.agent-reach/state/rate_limits.json` 中对应条目）。
- live 结果依赖 GitHub / Exa / Jina / SOV2EX / V2EX 的当前行为，上游变更
  需要更新对应平台的来源或解析器。
