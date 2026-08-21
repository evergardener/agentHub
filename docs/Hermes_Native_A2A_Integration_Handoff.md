# Hermes 原生 A2A ↔ agentHub 接入开发交接

> **已废弃（2026-08-21）**：固定 peer→worker 过渡方案仅供历史审计。
> 当前生产契约见 `Hermes_AgentHub_Profile_Integration.md`。

- 状态：proposed
- 日期：2026-08-18
- 目标：不用 MCP。通过 Hermes 已实现的原生 A2A client tools 接入 local-agent-system（agentHub）。

## 1. 结论

最终链路应为：

```text
qishuo Hermes 原生 A2A tools
  → Hermes A2A v1.0 JSON-RPC client
  → agentHub A2A compatibility endpoint（127.0.0.1:8310）
  → agentHub orchestrator / policy / audit
  → codex 或 kimi worker
```

本方案不新建 MCP adapter，不公开任何新端口，不走 Traefik，不开放 Web UI，也不允许 Hermes 直连 agentgateway、worker adapter、PostgreSQL 或 NATS。

## 2. 已核验的兼容性差异（必须先改）

Hermes 已有 A2A platform plugin 和 client tools（`a2a_discover`、`a2a_call`、`a2a_list`、`a2a_history`、`a2a_orchestrate`），但其现有 wire format 与 agentHub 当前 endpoint 不兼容，不能仅填一个 peer URL 就上线。

| 项目 | Hermes 原生 A2A client | agentHub 当前 orchestrator | 需要的改造 |
|---|---|---|---|
| JSON-RPC method | `SendMessage`（A2A v1.0） | `message/send` | agentHub 必须接受标准 `SendMessage`；保留 legacy `message/send` 兼容。 |
| request text Part | v1.0 `{ "text": "...", "mediaType": "text/plain" }` | 仅接受 `{ "kind": "text", "text": "..." }` | agentHub text extractor 同时接受 v1.0 member-presence `text` 和 legacy `kind:text`。 |
| auth header | `Authorization: Bearer <token>` | `X-Agent-Token` | agentHub 外部 A2A compatibility endpoint 接受标准 Bearer；遗留客户端可继续使用 `X-Agent-Token`。若两者同时出现且不一致则拒绝。 |
| agent card | 发现 `supportedInterfaces` 的 JSONRPC URL，fallback card `url` | 仅有 top-level `url`，无 `supportedInterfaces` | agentHub card 应声明 A2A v1.0 JSONRPC interface、版本和 endpoint URL。 |
| send result | A2A v1.0 `SendMessageResponse`：`result.task` 或 `result.message` | bare Task | compatibility endpoint 返回 v1.0 wrapper；legacy `message/send` 可保留 bare response。 |
| 目标 worker | Hermes `a2a_call` 参数为 peer + message，不发送 agentHub 私有 `metadata.agent` | agentHub 新任务要求 `metadata.agent` | 必须增加受控 peer→worker 路由机制，见 §4。 |
| task 获取 | Hermes 当前 native public tools 未提供 `tasks/get` | agentHub 支持 `tasks/get` | 第一阶段使用 Hermes native A2A 的会话/task reply；如需可靠长任务轮询，扩展 Hermes A2A plugin 的原生工具，而非引入 MCP。 |

## 3. 安全边界

- endpoint 固定为 `http://127.0.0.1:8310`，仅宿主机 loopback；不得接受模型提供的任意 URL。
- Hermes peer 必须在 `qishuo/config.yaml` 的 `a2a_agents` 中具名配置；禁止使用 native `a2a_call` 的 direct-URL 分支。
- 只允许逻辑 peers：`agenthub-codex`、`agenthub-kimi`；不得配置 `fake` 或通配能力 fan-out。
- 凭据使用 qishuo profile 私有 secret 注入；不得写入项目仓库、A2A messages、日志或命令行。
- agentHub compatibility endpoint 必须用独立的 caller token / caller identity 区分 qishuo，不能把 `LAS_GATEWAY_API_KEY` 当作 A2A peer token。
- 对 agentHub 写操作，服务端审批、审计和 artifact veto 仍是权威边界；Hermes 不能绕过。

## 4. Peer→worker 路由（阻塞设计项）

Hermes native `a2a_call(agent, message, context_id)` 不含 agentHub 私有的 `metadata.agent`。因此不能让模型在消息正文中声明“交给 codex”，也不能让 client 传任意 metadata。

推荐实现：**agentHub 依据认证的 peer identity 固定选择 worker**。

```text
Hermes peer identity: qishuo-codex  → agentHub worker: codex
Hermes peer identity: qishuo-kimi   → agentHub worker: kimi
```

实现要求：

1. agentHub 为两个 Hermes peers 发行不同 bearer token，或使用 token metadata / stable authenticated identity；不能信任 request body 自称的 peer/worker。
2. agentHub A2A compatibility handler 从认证 identity 解析固定 worker；忽略或拒绝与该 mapping 不一致的 `metadata.agent`。
3. Hermes `a2a_agents` 配置两个 peer，均指向同一 loopback URL，但各自具有独立 bearer credential、单一 capability 和固定 routing identity。
4. server-side mapping 只允许 `codex`、`kimi`；offline worker 返回稳定错误，不回退到其他 worker。

替代方案是扩展 Hermes native A2A plugin，使每个 configured peer 支持静态、受配置约束的 outbound metadata。该方案会修改 Hermes plugin，实现和回归面更大；优先选择 server-side identity mapping。

## 5. Approval protocol（Phase 0，阻塞上线）

agentHub 当前 follow-up 会对自然语言做子串匹配，且 approve 在 reject 前判断。`不批准` 等文本可能被错误当成批准，不能用于 Hermes 自动化链路。

必须把 approval 改为标准化的显式 A2A action：

```text
method: tasks/approve
params: {"id": "T-..."}

method: tasks/reject
params: {"id": "T-..."}
```

要求：

- 仅接受 task ID 和精确方法；禁止从 text Part 推断批准意图。
- 仅对处于 `input-required` 的指定 task 生效。
- 重复、晚到、终态 task 的操作返回稳定错误，不得再次委派。
- 审计记录 authenticated peer identity、task ID、action、timestamp；不记录 token。
- 现有 `message/send + metadata.taskId` 自然语言 follow-up 可以暂留给 legacy client，但必须标为 deprecated，并新增回归测试确保 compatibility endpoint 不走它。

## 6. 建议实施顺序

### Phase A：agentHub A2A v1.0 compatibility

修改 `src/orchestrator/a2a_server.py`（或拆出 compatibility module）：

1. 接受 `SendMessage` 和 legacy `message/send`。
2. 使用兼容 text extractor，支持 v1.0 text Part 与 legacy Part。
3. 兼容 endpoint 接受 `Authorization: Bearer`，并建立 qishuo peer identity。
4. agent card 增加 `supportedInterfaces`，声明 JSONRPC / A2A v1.0 URL。
5. 对 `SendMessage` 返回 `{"task": ...}` 或 `{"message": ...}` wrapper。
6. 实现 `tasks/approve`、`tasks/reject`，废弃 compatibility endpoint 的自然语言审批。
7. 实现 identity→worker 固定映射，仅支持 codex/kimi。

### Phase B：Hermes 原生 A2A peer registration

在 qishuo 的 `a2a_agents` 注册两个受限 peer（概念示例，token 使用环境引用，不写字面量）：

```yaml
a2a_agents:
  agenthub-codex:
    url: http://127.0.0.1:8310
    auth:
      type: bearer
      token: ${AGENTHUB_A2A_CODEX_TOKEN}
    timeout: 60
    capabilities: [code]
  agenthub-kimi:
    url: http://127.0.0.1:8310
    auth:
      type: bearer
      token: ${AGENTHUB_A2A_KIMI_TOKEN}
    timeout: 60
    capabilities: [research]
```

不要启用 `a2a_orchestrate` 对这两个 peers 的 capability fan-out，直到并发、任务配额和审批策略经过单独设计。

### Phase C：Hermes A2A client capability补齐（如确有需要）

Hermes 当前 native tools 适合 discover / send / conversation continuation，但未公开 `tasks/get`。若 agentHub 任务是异步的、需要可靠 status polling：

1. 在 Hermes 的 A2A plugin 中新增原生 `a2a_get_task` tool，输入仅为配置 peer 和 task ID。
2. 必须从 Agent Card/peer config 获取 endpoint；禁止 direct URL。
3. 支持 agentHub `tasks/get` 的结果，且不要把 artifact path 当作已验证文件内容。
4. 增加 bounded `a2a_wait_task` 仅在 get-task 稳定后考虑；最大等待时间有限，超时返回 last state，不重新提交。

这是 Hermes 既有 A2A plugin 的扩展，不是 MCP bridge。

## 7. 测试与验收

### agentHub contract tests

- Hermes v1.0 `SendMessage` request → accepted，并正确解析 text Part。
- legacy `message/send` request → 保持兼容。
- Bearer 正确/错误/缺失；X-Agent-Token legacy 兼容；两 header 冲突拒绝。
- card 包含 v1.0 JSONRPC `supportedInterfaces` 且 URL 为 loopback endpoint。
- `SendMessage` response 包装符合 Hermes `unwrap_send_message_response` 预期。
- qishuo-codex token 只能投递 codex；qishuo-kimi token 只能投递 kimi；伪造 metadata 不得改变 target。
- `tasks/approve` / `tasks/reject` exact action；自然语言模糊词不授权。
- offline worker、未知/过期 task、重复 approval/reject 具有稳定错误。

### Hermes integration acceptance

1. 配置 qishuo peer 后，`a2a_discover` 成功读取 agentHub card。
2. 使用 `a2a_call(agenthub-kimi, <只读任务>)`，确认任务进入 agentHub、目标 worker 为 kimi，并得到结果。
3. 使用 `a2a_call(agenthub-codex, <安全的写任务>)`，确认 `input-required`，再仅通过 `tasks/approve` 的受控 native tool 放行。
4. 若实现 `a2a_get_task`，交叉核对 A2A task 状态和 `agentctl-host.sh task show <id>`。
5. 验证 endpoint 仍仅监听 host loopback，未改 Traefik、Docker port publishing 或 Web UI exposure。

## 8. 回滚

- 从 qishuo `a2a_agents` 移除 `agenthub-codex` / `agenthub-kimi`；
- 撤销 agentHub 对应 qishuo peer tokens；
- 移除或禁用 agentHub compatibility route；
- 保持 worker、Compose、数据库与历史任务不变；
- 不以 direct worker / gateway / DB 调用作为降级路径。

## 9. 关键参考

```text
agentHub:
  docs/orchestrator-a2a.md
  src/orchestrator/a2a_server.py
  tests/unit/test_orch_a2a.py
  tests/contract/test_a2a_contract.py

Hermes native A2A:
  plugins/platforms/a2a/tools.py
  plugins/platforms/a2a/protocol.py
  plugins/platforms/a2a/security.py
  plugins/platforms/a2a/__init__.py
```
