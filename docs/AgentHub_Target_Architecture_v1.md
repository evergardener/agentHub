# agentHub 目标架构 v1

- 状态：Approved for implementation
- 日期：2026-08-19
- 决策基线：Hermes 主控、A2A 业务协议、agentgateway 网络治理、PostgreSQL 事实源

## 1. 逻辑架构

```text
User / WebUI / CLI
        |
        v
Hermes Runtime
  - Conversation Orchestrator
  - Planner / Reviewer
  - Approval Escalation
        |
        v
agentHub Control Plane
  - Collaboration / Task Engine
  - Agent Registry + Profile
  - Policy + ActionIntent
  - Context / Session Manager
  - Artifact / Audit
        |
        v
A2A Client -> agentgateway -> Agent Adapters -> Native Agents
        |              |
        +--- events ---+
               |
        NATS / JetStream -> State Writer -> PostgreSQL
               |                              |
               +-> SSE / WebUI                +-> recovery / history
               +-> OpenTelemetry / Jaeger
```

## 2. 组件边界

### Hermes Runtime

唯一正式总控。CLI 和 WebUI 都是 Hermes 的客户端，不再维护相互独立的对话状态。
Hermes 不直接访问 Worker endpoint、数据库表或 Agent 凭据，只调用控制面 API。

### Collaboration Engine

管理 Conversation、Collaboration、版本化 Task Plan/Step、Task、Message、revision、Session 和恢复。
所有状态变更必须幂等并写审计事件。

### Task Plan

Hermes 对多 Agent/多步骤目标必须先创建结构化 Task DAG。每个 Step 固定 Agent ID、
Agent Profile ID/version、依赖、预期 operation、产物和验收条件；这些字段进入 Task
上下文并随 A2A 委派/恢复发送给原生 Agent。Profile 漂移、Agent 替换、计划 superseded
或用户介入提升 revision 后，旧 Step 不得继续委派。超出 Step 预期 operation 的
ActionIntent 必须升级用户，不能借 Profile 的更大权限扩大当前任务范围。

### Agent Registry 与 Profile

Registry 记录在线 Agent Instance、Agent Card、租约、并发和能力；Profile 记录用户赋予的
职责、工具和权限。能力发现不等于授权。

### Policy 与 ActionIntent

Policy 使用结构化 operation/target/scope 判定。自然语言仅作为说明，不能成为最终授权依据。
未知操作 fail-closed。子 Agent 只能请求，Hermes/用户才能批准。

### A2A 与 agentgateway

A2A 是 Agent 间业务语义协议；agentgateway 是可替换的数据平面，负责认证、ACL、限流、
路由、TLS、重试和 OTel，不管理任务生命周期。开发环境允许受控直连作为诊断路径。

### Adapter

统一接口：

```python
start_session()
send_message()
stream_events()
list_pending_interactions()
respond_interaction()
continue_after_interaction()
resume_session()
pause()
interrupt()
cancel()
collect_artifacts()
```

Adapter 持久化 native_session_id 映射，翻译原生 CLI/服务事件，但不得自行批准 ActionIntent。
原生 approval/question 必须保留可回执 correlation ID；修改类放行只有在控制面提供已批准
ActionIntent receipt 后才能送回原生 Runtime。receipt 使用 HMAC 签名并绑定 task、interaction、
native rpc 和 context revision；拒绝和问答同样记录响应者与审计事件。

能力必须由 Adapter 明确声明。仅包装一次性 runner 的兼容 Adapter 必须声明
`multi_turn=false`、`native_resume=false`、`durable_session=false`；控制面不得把 task ID 或进程内
session ID 推断为可跨重启恢复的原生会话。若原生运行时不支持恢复，必须创建 replacement binding、
加载受控 Context Snapshot，并在审计时间线标记降级，不能伪装为续接原 session。

每次委派携带 `taskId`、`sessionId`、`nativeSessionId`、`contextRevision`、turn 幂等键和恢复模式。
Adapter 实例返回独立 `adapterInstanceId`。控制面仅在收到成功响应后推进 session turn 序号；响应丢失
时重放相同幂等键。原生恢复沿用当前 binding；非原生恢复必须创建带 `replacement_of_id` 的新 binding。
已结束的 Adapter Task 只有收到显式 `replaceSession=true` 才能重建，普通消息不得隐式复活终态任务。

## 3. 身份与权限

主体：user、hermes、agent、system。每个请求携带 caller identity、conversation、
collaboration、task、session、revision、trace 和 idempotency key。

权限层级：

```text
User authority
  -> Hermes delegated authority
    -> Agent task-scoped capability
```

授权必须绑定具体 operation、target scope、revision、过期时间和批准者。需求变化提升 revision 后，
旧 revision 未执行的修改授权失效。

## 4. 会话和消息一致性

消息先写 PostgreSQL，获得 conversation 内单调 sequence 后再投递。接收方 ACK 更新投递状态。
幂等键避免网络结果不确定时重复消息。

用户消息总能写入当前 revision；用户 steer/takeover 会原子提升 collaboration revision。
Hermes/Agent 基于旧 revision 提交上下文变更时返回 `context_conflict`，读取最新快照后重试。

运行时介入采用 capability 驱动：Codex 以当前 `turnId` 为前置条件调用原生
`turn/steer`，DSH 调用原生 `session.prompt(mode=steer)`；未声明 steer 的
Adapter（当前 Kimi ACP）不得由平台模拟“同 turn 纠正”，只能 interrupt 后开始
新 turn。介入消息先持久化为 `user.<mode>` 并提升 revision，再调用 Adapter；
Hermes 每轮会同步新增的用户介入消息，确保用户、Hermes 与子 Agent 看到同一纠偏。

## 5. Session 恢复

每个 Task 与 Agent 的绑定保存 native_session_id、resume_capability、最后消息和 Context Snapshot。
原生 resume 成功后继续同一 Session；无法 resume 时创建 replacement binding，并保留旧 binding、
恢复原因和上下文快照，不能静默伪装为原生连续会话。

## 6. Agent 间协作

Agent 只能向 Control Plane 发送 `request_collaboration`。控制面校验 Registry、Profile、配额、
可见上下文和权限后创建子 Task，并通过 gateway 投递给目标 Agent。

只读结果可以返回请求 Agent；修改建议必须先形成 ActionIntent。目标 Agent 也不能继承请求 Agent
未拥有的权限。

## 7. 中断语义

interrupt 采用渐进式处理：记录请求 -> 禁止新工具 -> 等待安全点 -> cooperative cancel -> SIGINT
-> 最终强制终止。每一步均产生事件，并保存部分产物、工作树 diff 和恢复快照。

涉及数据库迁移、Git 写入或部署时，Adapter 必须报告当前原子步骤；系统不得在未知一致性状态下
宣称任务已取消完成。

## 8. 可观测性

用户时间线与运维 Trace 分离：

- 用户时间线：实际消息、方案、工具摘要、审批、产物、复审和控制操作；
- Trace：Hermes、A2A、gateway、Adapter、LLM、tool、DB span；
- 原始日志：受保留期、大小、脱敏和访问控制保护的 Artifact。

不得记录隐藏思维链、明文密钥或完整敏感工具参数。

## 9. 数据边界

PostgreSQL 是 Conversation、Collaboration、Task、Message、Session、ActionIntent、Approval、
Artifact 元数据和事件的事实源。NATS 只负责实时分发和解耦，不能成为唯一历史来源。

Workspace/Git 保存实际文件产物；数据库保存路径、哈希、版本、来源和可见性。

## 10. 兼容与迁移

- 保留现有 Task 状态机，新增 Collaboration phase，避免一次性破坏协议；
- legacy `message/send` 只做迁移兼容，不扩展新能力；
- 新能力基于稳定 A2A task/context/message 语义；
- 现有 Codex/Kimi Adapter 逐个迁移到统一 Session Adapter 接口；
- DSH 作为第一个新 Adapter 验证插件化边界。DSH `headless` 仅用于一次性
  新会话；生产 Session Adapter 使用回环 DSH Web API，以原生 session id
  实现多轮、历史读取、恢复、事件追踪和取消；新建 Hub session 默认在
  DSH 原生层固定 `read-only`，避免仅有控制面 Profile 而执行层仍可写；
  DSH 下行使用 `/api/events.mux` SSE（与其 WebSocket carrier 共用相同
  ServerRequest 语义），`/api/respond` 继续同一原生 turn；待处理交互同时
  写入 `agent_session_interactions` 并关联 ActionIntent；
- Kimi 生产 Adapter 使用 `kimi acp`，不使用无法回传逐工具审批的 prompt
  CLI。ACP `session/request_permission` 进入同一 PendingInteraction/
  ActionIntent/签名 receipt 链，`allow_once` 或 `reject_once` 回应原 RPC 后
  继续同一 native session/turn；`session/update` 仅保存有界、脱敏的工具摘要
  与 assistant 输出，不保存隐藏思维链；
- Codex 生产 Adapter 使用 `codex app-server`，不使用只能事后观察工具事件的
  `exec --json`。thread 新建与恢复均固定 read-only，并将
  `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、
  `item/permissions/requestApproval` 映射到统一 PendingInteraction/ActionIntent/
  签名 receipt 链；允许只回答原生同一 RPC 的一次性 `accept` 或 turn-scope
  精确权限，禁止 session 级缓存授权；
- 所有支持 streaming 的 Adapter 把原生消息增量、工具生命周期、计划、turn
  和交互事件转换为 SessionEvent；Adapter Server 统一发布
  `agent.session.event`，载荷保留 nativeEventType/session/native session
  correlation。State Writer 用全局单调 `seq` 落库，WebUI SSE 以 `after`
  游标实时推送和断线补发；隐藏推理与原始工具输出不得进入此时间线；
- gateway 静态路由逐步替换为 Registry 驱动的稳定逻辑路由。
