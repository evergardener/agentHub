# agentHub 产品需求文档 v1

- 状态：Approved for implementation
- 日期：2026-08-19
- 适用范围：agentHub 下一阶段开发与生产验收
- 关联文档：`AgentHub_Target_Architecture_v1.md`、`AgentHub_Implementation_Plan_v1.md`

## 1. 产品定位

agentHub 是一个 Hermes 主控的多 Agent 协同平台。用户只需要与 Hermes 交互；
Hermes 负责理解需求、拆解工作、选择 Agent、组织多轮技术确认、管理审批、监督执行、
发起复审并向用户汇报。

Codex、Kimi、DeepSeek Harness（DSH）以及后续的 Pi 等 Agent 保持独立部署和独立使用
能力。接入 agentHub 后，Adapter 只负责协议转换、会话恢复、事件采集和权限隔离，
不得抹平各 Agent 的原生工作方式、工具、上下文和专长。

## 2. 核心原则

1. **控制权顺序固定**：用户 > Hermes > 子 Agent。
2. **Hermes 是唯一用户总控**：子 Agent 不直接向用户声明任务最终完成。
3. **协作必须经过 agentHub**：子 Agent 不得绕过 Registry、Policy 和审计直接调用其他 Agent。
4. **修改操作必须授权**：子 Agent 只能提交结构化 ActionIntent，不能批准自己的操作。
5. **会话必须可恢复**：跨进程、跨日期继续工作时必须恢复同一 Collaboration 和 Agent Session。
6. **事实先持久化再投递**：消息、用户介入、审批和执行意图先写 PostgreSQL，再发送给 Agent。
7. **全程可追溯**：消息、工具、审批、产物、复审和用户介入均有稳定 ID、顺序和审计记录。
8. **不展示隐藏推理**：WebUI 展示实际收发消息、显式方案和工具证据，不展示模型隐式思维链。

## 3. 角色与职责

### 3.1 用户

- 设置项目级 Agent Profile；
- 与 Hermes 对话；
- 审批 Hermes 无法确认或策略要求升级的操作；
- 查看实时协作过程；
- 对 Agent Session 执行 comment、steer、pause、interrupt、cancel、takeover；
- 撤销长期授权；
- 对最终结果作最终接受判断。

### 3.2 Hermes

- 建立 Conversation 和 Collaboration；
- 拆解任务、建立依赖和验收标准；
- 根据 Profile、能力、在线状态、并发和风险选择 Agent；
- 要求实施 Agent 先提交方案、风险、问题和产物计划；
- 在同一上下文中与 Agent 多轮确认；
- 审批授权范围内的 ActionIntent；
- 将高危、模糊、越权或不可回滚操作升级给用户；
- 监督执行和恢复；
- 委派独立 Reviewer；
- 基于真实产物和复审证据向用户汇报。

### 3.3 子 Agent

- 保留原生 CLI、模型、工具和 Session；
- 接收 Hermes 或经策略允许的其他 Agent 发起的协作请求；
- 主动报告方案、风险、阻塞和澄清问题；
- 只在获批的 ActionIntent 范围内执行修改；
- 生成结构化进度、工具和产物事件；
- 接受用户/Hermes 的 steer、pause、interrupt 和 rework；
- 不得创建长期授权、扩大权限或绕过 agentHub 调用其他 Agent。

## 4. 核心领域对象

| 对象 | 含义 | 生命周期 |
|---|---|---|
| Conversation | 用户与 Hermes 的长期对话 | 可跨项目和多天 |
| Collaboration | 一次完整目标或项目协作 | 从规划到最终汇报 |
| Task Plan / Step | Hermes 的版本化任务 DAG 与执行契约 | 激活、失效、重规划 |
| Task | 可独立执行和验收的工作单元 | 属于一个 Collaboration |
| Agent Session | Task 与某 Agent 原生 Session 的绑定 | 可暂停、恢复或重建 |
| Message | 用户/Hermes/Agent/System 的持久化消息 | 有单调顺序和投递状态 |
| Context Snapshot | 某 revision 的结构化上下文快照 | 关键决定后生成 |
| ActionIntent | 子 Agent 请求执行的结构化操作 | 必须经过权限链 |
| Tool Execution | 一次可观察的工具调用 | 关联审批、输出和影响 |
| Artifact | 可验证产物 | 有版本、哈希和来源 |
| Agent Profile | 用户给某类 Agent 的角色和边界 | 版本化、可回滚 |

## 5. 标准工作流

1. 用户向 Hermes 提交目标。
2. Hermes 创建 Collaboration，并形成结构化 Task Plan/DAG；每步绑定 Agent/Profile
   version、依赖、预期 operation/产物和验收条件。
3. Hermes 向实施 Agent 发送完整 Step 执行契约，而不是只发送自然语言 objective。
4. Agent 在同一 Session 返回技术方案、风险、依赖、问题、预计操作和产物。
5. Hermes 与 Agent 在同一 task/context 内多轮确认；必要时询问用户。
6. 方案确认后 Task 进入 ready。
7. Agent 对每类修改提交 ActionIntent。
8. Policy 先检查用户授权、Profile、项目范围、工具和路径；再由 Hermes 或用户批准。
9. Agent 执行并实时上报消息、工具、进度和产物。
10. Reviewer 读取任务上下文、diff、测试、产物和必要消息，提出接受或返工意见。
11. Hermes 处理返工，最终基于证据向用户汇报。

## 6. Agent 发现与协作

子 Agent 可通过平台执行：

- `list_agents`
- `get_agent_card`
- `request_collaboration`
- `send_collaboration_message`
- `create_child_task`
- `request_action_approval`
- `report_blocked`
- `request_review`

所有调用必须使用逻辑 Agent ID，由 agentHub Registry 解析实际实例和 endpoint。
调用方不获得目标 Agent 的直连凭据。

只读咨询可在策略允许时自动执行。任何可能改变文件、Git、数据库、依赖、进程、服务、
网络外部状态或授权的操作，都必须生成 ActionIntent 并上报 Hermes。

## 7. ActionIntent 与审批

ActionIntent 至少包含：

- operation；
- project；
- targets（路径、服务、仓库、数据库对象等）；
- purpose；
- expected_effects；
- rollback_plan；
- requested_by；
- task/session；
- based_on_revision；
- risk。

Hermes 可以批准用户已授权且在 Profile/项目边界内、影响清晰、可回滚的操作。
以下操作必须升级给用户：

- 删除、生产变更、外部发布；
- 访问工作区外资源；
- 密钥、凭据、权限提升；
- 创建或扩大长期授权；
- 无可靠回滚；
- Agent 意见冲突；
- 任务范围扩大或需求含糊；
- 当前上下文无法判断实际影响。

未识别操作必须 fail-closed：进入 ask/blocked，不能默认视为只读。

## 8. 会话连续性与上下文

平台必须持久化：

- conversation_id；
- collaboration_id；
- task_id；
- agent_session_id；
- native_session_id；
- context_revision；
- last_message_seq；
- resume_capability；
- context_snapshot。

恢复顺序：

1. 优先调用 Agent 原生 resume；
2. 不支持原生 resume 时，根据结构化 Context Snapshot 重建；
3. 不允许把恢复任务伪装为新任务；
4. 不允许因 Hermes、Adapter 或 WebUI 重启而重复执行已完成操作。

Context Snapshot 至少包含当前目标、已确认约束、关键决定、未解决问题、用户最新指令、
已执行操作、产物状态、审批范围、禁止事项和下一步。

## 9. 用户实时介入

| 操作 | 语义 |
|---|---|
| comment | 补充信息，不主动打断当前工具 |
| steer | 修改方向，在安全检查点应用 |
| pause | 阻止新的工具调用 |
| interrupt | 协作式中断当前执行，必要时逐级终止 |
| cancel | 取消 Task，并记录部分产物和清理状态 |
| takeover | 用户直接向指定 Agent Session 发送指令 |
| return_to_hermes | 将控制权交回 Hermes 并触发重新规划 |

用户消息必须先落库并提升 context_revision。Hermes 和相关 Agent 都必须收到该消息。
旧 revision 上未开始的写许可自动失效；受影响任务进入 paused 或 needs_replan。

## 10. 实时可观测性

WebUI 用户时间线展示：

- 用户、Hermes、Agent 的实际消息；
- 方案、澄清和风险；
- 审批与授权范围；
- 工具名称、状态、耗时和脱敏参数；
- Artifact、Git diff、复审和返工；
- Agent Session 控制者和中断状态。

运维侧使用 OpenTelemetry/Jaeger 追踪 Hermes、A2A、gateway、Adapter、LLM、工具和数据库。
实时事件通过 NATS 分发、PostgreSQL 补发，WebUI 使用带 Last-Event-ID 的 SSE。

目标：本机事件展示 p95 小于 2 秒；刷新或断线后不丢失、不重复展示。

## 11. Agent Profile

Profile 至少支持：角色、职责、可执行/只读、工具、工作区、任务类型、复审关系、模型、
成本、优先级、超时、并发、审批级别、启用状态、项目覆盖和版本回滚。

Profile 修改必须审计。自然语言角色描述不能覆盖服务端权限边界。

## 12. 生产验收

发布前必须满足：

- 同一任务跨日期恢复同一 Agent Session；
- 用户介入后旧 revision 写操作不能继续；
- 子 Agent 可以发现并请求其他 Agent 协作，但不能绕过审批；
- Hermes、gateway、NATS、Adapter 任一重启不丢任务或重复执行；
- 完整消息、审批、工具、产物和复审链可追溯；
- 无自然语言审批绕过；
- 备份恢复、升级和回滚经过实测；
- 至少完成 10 个单 Agent、10 个多 Agent、5 个多轮澄清、5 个审批、5 个返工任务。
