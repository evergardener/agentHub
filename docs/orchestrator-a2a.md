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

Card 声明 A2A v1.0 JSON-RPC interface，能力包括 Registry 发现、编排、审批和
持久监督。监督是 agentHub 扩展控制包，不冒充 A2A 标准 push notification。

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
{"agenthub":"v1","action":"tasks/create","agent":"dsh","title":"简洁任务标题","summary":"面向用户的简要说明","objective":"完整未总结指令","project":"optional","workspace":"/absolute/project/path"}
```

Codex 任务可选指定 Profile 允许的模型与推理强度：

```json
{"agenthub":"v1","action":"tasks/create","agent":"codex","model":"gpt-5.6-luna","reasoning_effort":"max","title":"实现指定变更","summary":"按高推理强度执行并验证。","objective":"完整未总结指令","workspace":"/absolute/project/path"}
```

`model` 和 `reasoning_effort` 会进入任务 `runtime_config`、协作消息、审计事件及
native session 快照。二者必须位于当前 Codex Agent Profile 的
`allowed_models` / `allowed_reasoning_efforts`；Codex Adapter 还会通过原生
`model/list` 校验组合并核对 `thread/start|resume` 返回的实际生效值。Profile
未授权、模型不支持、推理强度不支持、恢复中的配置变化或非 Codex Adapter 都会
fail-closed。省略字段时继续使用 Adapter/Codex 默认配置。

Orchestrator 每次从 Registry 解析 `agent`。未知、offline 或 disabled 都稳定失败，
不回退到其他 Agent。读操作通常返回 `submitted`；需审批的操作返回
`input-required`，审批前不调用 Adapter。

涉及现有代码仓库的任务必须提供 `workspace`，且必须为绝对、非根目录路径；
不得只把路径写入 `objective`。纯隔离任务可省略。指定后写入 Task 持久上下文并随
Adapter 消息传递。DSH Adapter 调用原生 `workspace.create({path})` 注册或复用
Workspace，再通过 `session.create({workspaceId})` 建立可被 DSH 侧边栏正确分组
的 Session；Codex Adapter 则把同一路径同时设置为原生 thread 的 `cwd` 和
`runtimeWorkspaceRoots`，并在新建、恢复和进程重连时核验 App Server 回报的 cwd。
未指定时继续使用隔离的 `AgentWorkspace/tasks/<task_id>`，不会把多个任务混入一个
共享目录。Workspace 只定义执行与路径校验边界，不等于写权限：原生 Session 仍以
read-only 启动，每次修改必须产生可检查的 ActionIntent；仅当操作在 Agent Profile
allowlist、目标位于 workspace 且有可验证的回滚方案时，Hermes 才可批准一次。
同一 native session 的 workspace 被固定；路径发生变化时必须创建替代 Session。
`title` 与 `summary` 分别用于 WebUI 的简洁目标和说明；完整审计指令仍保存在
`objective`。Hermes 不应把 commit SHA、完整路径或整段证据清单复制进 `title`。

### 查询、批准和拒绝

```json
{"agenthub":"v1","action":"tasks/get","task_id":"T-..."}
```

`tasks/get` 和原生 interaction 控制包必须沿用创建任务时的同一个
`contextId`；服务端同时校验已认证 peer、context 对应的 Collaboration 与
task 所属关系。跨 peer/context 的查询或响应会 fail closed。
对 wrapped `SendMessage`，`contextId` 在 message 上；对直接 JSON-RPC 方法，
`params.contextId`（兼容 `context_id`）必填。legacy `message/send` 不属于这条
Hermes control-plane 路径。

需要单独查看某个原生交互时：

```json
{"agenthub":"v1","action":"interactions/get","interaction_id":"INT-..."}
```

`interactions/get` 返回与 `tasks/get.metadata.pending_interactions` 相同的
有界审计视图。对 `command.read`，视图至少包含 `inspectable`、规范化的
`command`/`args`、`cwd`、`workspace`、`rollback_plan`、`allowed_responses`、
`risk`、`policy_route` 和 `awaiting_hermes`/`awaiting_user`。命令参数超过边界、
含凭据模式或无法结构化识别时，命令细节不会被当作可批准内容，`inspectable`
为 `false`；不会把原始 adapter payload 透传给 Hermes。

```json
{"agenthub":"v1","action":"tasks/approve","task_id":"T-..."}
```

```json
{"agenthub":"v1","action":"tasks/reject","task_id":"T-..."}
```

原生 Agent 阻塞时，`tasks/get` 在 `metadata.pending_interactions` 返回可审查详情。
只有 `inspectable=true` 且 `action_intent_status=awaiting_hermes` 的请求可由 Hermes
回复；`awaiting_user` 必须交给 WebUI 用户。Hermes 回复格式：
受限 Docker 只读发现会结构化为 `command.read` / `risk=read`，并且仍通过
`awaiting_hermes` 签发一次性 receipt；这类操作的 `rollback_plan=null` 表示
无变更、回滚不适用，不能扩展为任意 shell 自动放行。

```json
{"agenthub":"v1","action":"interactions/respond","interaction_id":"INT-...","outcome":"allowed-once","note":"已核对目标、影响与回滚"}
```

`tasks/approve` / `tasks/reject` 只处理任务委派前的 delegation gate，不能替代
原生 interaction 的 `interactions/respond`，也不能为 `awaiting_user` 的 ActionIntent
提供 Hermes 批准。`allowed-once` 会绑定 task、interaction、native request/session
和 context revision 的一次性 receipt；重复、晚到或跨 context 的响应稳定失败。

重复、晚到或终态审批稳定失败，不重复委派。A2A Task 的
`status.message` 是标准 Message 对象，并始终显式包含 `task_id=T-...`。
这是 Hermes 原生 renderer 不展示结构化 `task.id` 时的稳定续接标识。

### 异步监督与原会话唤醒

qishuo profile-local `agenthub-supervisor` plugin 会在成功的 `tasks/create` 工具
结果后自动登记 watch。canonical `agent:` Gateway route 会把 `watch_id` 与
发起任务的 Gateway session、A2A `context_id` 持久绑定；Hermes WebUI 的
`agent_bridge` route 使用原生 durable async-completion queue 将唤醒送回原
`mt...` session。普通 CLI/TUI route 仍只在当前 Hermes 进程内轮询和注入，进程
退出后明确不可恢复，不得宣称 durable supervision。Gateway poller 不得认领
`agent_bridge` watch，WebUI route 也不得伪装成 `agent:` Gateway session。
agentHub 对以下状态写入持久 outbox：委派审批、worker 原生
交互、blocked、failed/cancelled，以及等待用户验收。Task 状态、告警和 outbox
在 State Writer 的同一事务提交；写 outbox 失败时不会留下已前进却无人通知的状态。

Plugin 只轮询自己已知的 `watch_id`，服务端仍校验 authenticated peer、Task
所属 Collaboration 与 context。通知采用租约投递并在 ACK 前重试；重复 ACK
幂等。唤醒 envelope 只能包含 `notification_id`、`watch_id`、`task_id`、
`context_id`、`event_type` 和 `internal_status`，禁止包含 objective、worker 回复、
工具参数或审批 payload，避免把远端内容直接注入 Hermes。Hermes 被唤醒后必须先用
同一 `context_id` 调用 `tasks/get` 获取权威状态，再按上节权限处理，并在完成汇报
后调用 plugin 的 `agenthub_supervision_ack`。同一 `notification_id` 在 ACK 前
只创建一个 native async completion，dispatch 失败则保持 outbox 可重试。
`awaiting_user` 只能通知用户或等待
WebUI 操作；Hermes 不得据此自批。最终结果必须由用户显式 `tasks/accept`，不得由
后台 supervisor 自动验收。

监督控制包供 profile plugin 使用，常规对话不应手写：

```json
{"agenthub":"v1","action":"supervision/register","task_id":"T-..."}
{"agenthub":"v1","action":"supervision/pull","watch_ids":["WATCH-..."],"limit":20}
{"agenthub":"v1","action":"supervision/ack","notification_id":"SN-..."}
{"agenthub":"v1","action":"supervision/stop","task_id":"T-..."}
```

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
