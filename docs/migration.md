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
      `cooldowns reset --platform <id>`）
- [ ] 至少一个 search fixture 与一个 read fixture
- [ ] 删除目录后其余平台的测试仍然通过
