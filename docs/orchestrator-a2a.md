# agentHub 外部 Hermes A2A 契约

当前生产入口是 `http://127.0.0.1:8300/agenthub`。`8310` 只是 Docker
loopback 诊断端口，qishuo 不得将它作为生产 peer。

## 架构

```text
qishuo Hermes
  -> agentgateway /agenthub
  -> Orchestrator A2A Server
  -> Registry / enabled gate / approval policy / task state
  -> agentgateway /agents/{agent_id}
  -> Registry-resolved Adapter
```

agentgateway 处理认证、ACL、限流、超时、重试和观测。Registry 处理 Agent
发现。qishuo 只有一个 `agenthub` peer，不为 worker 配置 peer 或 token。

## 认证

| 路由 | 身份 | 权限 |
|---|---|---|
| `/agenthub/**` | `LAS_HERMES_GATEWAY_API_KEY` | 只允许 `role=hermes` |
| `/agents/**` | `LAS_GATEWAY_API_KEY` | 只允许 `role=orchestrator` |
| Orchestrator A2A | gateway 注入 `LAS_HERMES_BACKEND_TOKEN` | token→peer，不绑 worker |
| Adapter | `LAS_ADAPTER_TOKEN` | 由 Registry 动态代理透传 |

qishuo 只持有 `LAS_HERMES_GATEWAY_API_KEY`。gateway 验证该身份后删除/替换
外部 Authorization，以 `LAS_HERMES_BACKEND_TOKEN` 访问 Orchestrator；后者是
`LAS_A2A_PEERS` 的 key，不得交给 qishuo。两枚 token 必须不同。

## Agent Card

```http
GET /agenthub/.well-known/agent-card.json
Authorization: Bearer <qishuo-token>
```

Card 声明 A2A v1.0 JSON-RPC interface，能力包括 Registry 发现、编排和审批。

## Hermes 原生 SendMessage 控制包

Hermes v0.20.4 的 `a2a_call` 不允许添加自定义 `metadata.agent`，因此
`SendMessage` 的单一 text Part 必须是严格 JSON。根对象必须含
`"agenthub":"v1"`。

### 发现

```json
{"agenthub":"v1","action":"agents/list"}
```

返回 A2A Message，text Part 是 JSON：

```json
{"agents":[{"id":"codex","enabled":true,"online":true,"skills":[],"profile_id":"backend"}]}
```

`enabled=false` 的 Agent 不参与探测或委派。

### 创建任务

```json
{"agenthub":"v1","action":"tasks/create","agent":"dsh","objective":"完整未总结指令","project":"optional"}
```

Orchestrator 每次从 Registry 解析 `agent`。未知、offline 或 disabled 都稳定失败，
不回退到其他 Agent。读操作通常返回 `submitted`；需审批的操作返回
`input-required`，审批前不调用 Adapter。

### 查询、批准和拒绝

```json
{"agenthub":"v1","action":"tasks/get","task_id":"T-..."}
```

```json
{"agenthub":"v1","action":"tasks/approve","task_id":"T-..."}
```

```json
{"agenthub":"v1","action":"tasks/reject","task_id":"T-..."}
```

重复、晚到或终态审批稳定失败，不重复委派。A2A Task 的
`status.message` 是标准 Message 对象，并始终显式包含 `task_id=T-...`。
这是 Hermes 原生 renderer 不展示结构化 `task.id` 时的稳定续接标识。

`/agents/**` 经 Orchestrator 动态代理时同时携带两个不同用途的凭据：Bearer
只认证 agentgateway，`X-Agent-Token` 只透传给 Adapter。Orchestrator 仅在
`/worker-proxy/**` 接受这种双身份形状；其他控制面路径仍拒绝冲突 header。

## Context 与审计

qishuo 对同一协作持续传递 `context_id`。Orchestrator 将
`(已认证 peer, contextId)` 映射为稳定的 Conversation/Collaboration；相同 peer
和 context 会复用同一会话，不同 peer 即使发送相同 context 也不会串话。映射使用
哈希后的内部 ID，不把调用方字符串直接作为主键。若请求没有 `contextId`，
agentHub 生成一个并在 Task 中回显，调用方后续必须续传该值。

`tasks/create` 在创建 Task 时写入 `tasks.collaboration_id`，并保存一条未总结的
`a2a.task.request` 消息和 `task.a2a_context.bound` 审计事件。因此 WebUI 可从
Session 看到后续状态、审批、Adapter run、结果、事件和 artifacts。初始
`submitted` 不代表已完成；Hermes 必须查询终态和产物后再汇报。
未写入静态 catalog 的新 Agent 可凭有效 Registry lease 自动出现，无需
为 Hermes 增加 peer 或路由；Registry-only Agent 租约过期后从发现结果移除，
避免历史测试 worker（如 `fake`）或已卸载 worker 持续暴露给 Hermes。
完成事件的 `result_summary` 从 worker 的规范 `last-message.md` 提取，最多
4000 字符，用于 Task 列表、状态查询和 A2A 简要结果；完成事件另携带最多
200000 字符的 `result_text`，State Writer 将其作为 `agent.task.result` 写入持久
对话，因此 WebUI 对话区不再用摘要替代 Agent 原始回复。更长输出会在对话中明确
标记，并以 `last-message.md` 为准。路径必须位于配置的 workspace 内，否则只返回
不含内容的 artifact 提示，避免越界读取。

DSH Adapter 对 canonical `last-message.md` 保留脱敏后的完整回复，安全上限为
1000000 字符；`dsh-history.json` 默认最多保留 5000 个原生事件，并写入
`totalEvents`、`retainedEvents`、`eventTruncated`、`fieldTruncated` 与
`truncated`，达到事件或字段上限时必须显式标记，禁止静默截断。原生 history
请求最多读取 1000 条消息。以上限制用于控制 NATS payload、数据库消息和
artifact 的资源占用，三者不得再共用 4000/8192 字符的展示摘要限制。

## Legacy

`message/send` + `X-Agent-Token` + `metadata.agent` 仅保留给旧本地 client。
qishuo 生产调用不得使用 legacy 路径，也不得在 agentHub 失败时直调
worker Adapter/CLI。legacy 路径不自动创建 Collaboration，避免把身份不明确的
旧调用混入 qishuo 会话。

## 验收

```bash
.venv/bin/python -m pytest tests/unit/test_orch_a2a_v1.py \
  tests/unit/test_agentgateway_config.py -q
```

生产验收和 qishuo 备份/回退流程见
`docs/Hermes_AgentHub_Profile_Integration.md`。
