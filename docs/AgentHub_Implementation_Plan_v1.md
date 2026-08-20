# agentHub 生产化实施计划 v1

- 状态：In progress
- 日期：2026-08-19
- 原则：先领域模型与安全，后实时 UI 和 Agent 扩展

## Phase 0：文档与边界收口

交付：产品需求、目标架构、实施计划、ADR；确定唯一 Hermes Runtime、权限层级、会话恢复和
用户介入语义。

验收：所有需求均映射到可测试条目；不存在两个互不共享状态的正式 Hermes 总控。

## Phase 1：事实源与安全基线

### 1A 协作数据模型

- conversations / collaborations；
- conversation_messages；
- agent_session_bindings；
- action_intents；
- tasks 关联 collaboration；
- 单调消息 sequence、context_revision、idempotency key；
- SQLite/PostgreSQL 双迁移和存取测试。

### 1B 权限收紧

- 未识别操作 fail-closed；
- 建立结构化 risk/action 类型；
- 逐步替换关键词审批；
- 授权绑定 operation/target/revision；
- CI 测试成功后才能构建和发布镜像。

验收：模糊、英文或同义改写不能自动取得写权限；消息可按 sequence 重建完整历史。

## Phase 2：Agent SDK、Registry 与 Profile

- 定义 Session Adapter 接口；
- 迁移 Codex/Kimi；
- 实现 Agent Template/Profile/Version；
- WebUI Profile 编辑和审计；
- Registry 驱动路由；
- 接入 DSH；
- 删除 Hermes 工具中的 Codex/Kimi 枚举限制。

验收：新增 Pi/Fake Adapter 不修改 Hermes 核心；Agent 独立运行和受调度运行均通过。

## Phase 3A：持久 Session 与上下文恢复

- native_session_id 捕获和 resume；
- Context Snapshot；
- session replacement 审计；
- Hermes/Adapter 重启恢复；
- context revision 冲突控制；
- 防止重复执行。

验收：上午任务在全部服务重启后，第二天继续同一 collaboration/task/session；不产生重复任务。

## Phase 3B：多 Agent 协作与用户介入

- Agent 发现和 request_collaboration；
- 子 Task 与权限不继承原则；
- Agent 主动澄清；
- ActionIntent 审批升级；
- comment/steer/pause/interrupt/cancel/takeover；
- 用户介入后 Hermes needs_replan；
- Reviewer 返工闭环。

验收：Codex 请求 Kimi 只读协作成功；任何修改请求被 Hermes/用户权限链拦截；用户 steer 后旧
revision 写许可失效。

## Phase 4：实时 WebUI 与追踪

- Hermes Web 聊天；
- Conversation/Collaboration/Task/Session 树；
- NATS 到 SSE 实时桥；
- Last-Event-ID 断线补发；
- 消息、工具、审批、Artifact、diff 和复审时间线；
- Session 控制面板；
- OTel GenAI/A2A/tool spans；
- 内容脱敏与保留策略。

验收：本机实时事件 p95 < 2 秒；刷新、重连不丢失或重复；不暴露隐藏推理和密钥。

## Phase 5：生产部署加固

- [x] WebUI 身份认证、签名 HttpOnly Cookie、CSRF、RBAC，以及非 loopback
  无认证启动失败；compose 默认 fail-closed；
- [x] 外部 Hermes A2A 入口生产强制认证、弱 token/非 loopback 无认证启动
  失败，并使用常量时间凭据比对；
- [x] 生产配置预检覆盖 `.env` 权限、默认/弱密钥、WebUI roles 和 A2A peer
  映射，检查输出不泄露 secret value；
- [ ] gateway：loopback 剖面已完成 strict API key、路由 ACL、独立 token-bucket
  限流；跨主机 TLS 1.3/mTLS + strict JWT/CEL 剖面、Hermes JWT 文件轮换与
  mTLS 客户端、独立 Compose 和 fail-closed 预检已实现；2026-08-20 使用临时 CA、
  JWKS/JWT、随机端口和 fake worker 完成本机隔离安全剖面验收（5 passed）：mTLS
  缺证书拒绝、JWT 401、claim ACL 403、A2A 委派及 Hermes JWT 文件轮换撤权/恢复；
  待真实 CA/OIDC 与第二主机重复 401/403/轮换/委派演练后勾选；
- [ ] 供应链实现已完成：外部镜像 tag + OCI digest、GitHub Actions commit SHA、
  agentgateway 二进制 SHA256 固定；发布生成 SBOM/provenance，经 Trivy 门禁后以
  Cosign OIDC 签名。待首次远端 workflow 成功并验证签名/attestation 后勾选；
- [x] WebUI/Orchestrator readiness、State Writer/Janitor 依赖探针、Gateway
  端口探针、Compose 健康依赖、资源上限与容器日志轮换；
- [ ] 告警实现已完成：持久去重 outbox、WebUI 确认、可选 HTTPS webhook、
  指数退避及三次失败 critical 升级；2026-08-20 已以临时自签 CA、随机 loopback
  端口和临时数据库完成本机真实 HTTPS 503 三次失败升级、204 恢复及成功后不重复
  投递门禁（1 passed）；待目标环境配置正式证书/webhook 并重复失败/恢复演练后勾选；
- [x] PostgreSQL custom dump、JetStream、agent-data、宿主机 Workspace 一致性
  备份与离线完整性校验；
- [x] 受显式确认保护的自动化恢复：恢复前 safety backup、PG exit-on-error、
  卷替换、原 Workspace 可恢复保留、失败保持停机；
- [x] 隔离 Compose 栈真实恢复演练：PostgreSQL、NATS、agent-data、Workspace
  mutated→original，safety backup/旧 Workspace 可读，临时资源完整清理；
- [x] 数据库 migration 前强制验证一次性、新鲜备份回执；迁移前原子消费，
  成功删除，异常恢复供重试，崩溃遗留 consuming 状态拒绝复用；
- 故障注入、升级和回滚演练。

验收：Hermes、gateway、NATS、DB、Adapter 任一重启不丢任务或重复写操作；备份可实际恢复。

## Phase 6：真实任务试运行

- 10 个单 Agent 任务；
- 10 个多 Agent 协作；
- 5 个多轮澄清；
- 5 个审批；
- 5 个 Reviewer 返工；
- 3 个故障恢复。

发布门：无审批绕过、重复执行、消息丢失或产物丢失；多 Agent 对质量有可观察提升。

## 当前迭代（Iteration 1）

1. [x] 完成本文件及关联需求/架构/ADR；
2. [x] 新增协作事实源 migration 004；
3. [x] 新增 collaboration_store 最小存取接口；
4. [x] 实现单调消息序号、session binding 和用户介入 revision；
5. [x] 将未知审批分类改为 fail-closed；
6. [x] 运行离线单元/契约测试并修复回归；
7. [x] 将 Hermes chat 接入持久 Conversation/Collaboration；
8. [x] CI 增加 unit/contract/PostgreSQL 门禁，镜像构建依赖测试成功；
9. [x] 为 ActionIntent 增加结构化 Policy 判定与审计事件。

## 当前迭代（Iteration 2）

1. [x] 新增精确 operation ID 的 `action_permissions.yaml`；
2. [x] 新增 ActionPolicy，未知 operation 默认升级用户；
3. [x] 工作区内且可回滚的修改路由 Hermes；
4. [x] 越界、无回滚、删除、推送、部署、数据库、密钥操作路由用户；
5. [x] ActionIntent 支持 `pending → awaiting_hermes/awaiting_user/approved/rejected`；
6. [x] Hermes 无权批准 `awaiting_user`；
7. [x] 消息、用户介入、ActionIntent 创建/路由/决策写统一审计事件；
8. [x] 完整 unit+contract 回归通过（173 passed）。

## 当前迭代（Iteration 3）

1. [x] 定义统一 Session Adapter SDK，覆盖 start/send/stream/resume/pause/interrupt/cancel/artifact；
2. [x] 能力显式声明 multi-turn、resume、native resume、durable session、streaming 和控制能力；
3. [x] Codex/Kimi 通过 one-shot 兼容层运行，不虚报 native resume 或 multi-turn；
4. [x] Fake Adapter 支持同一 task/session 的多轮消息、暂停、恢复、中断和取消；
5. [x] Adapter 拒绝旧 context revision，暂停/取消后的晚到结果不得恢复任务；
6. [x] migration 005 建立 Agent Template/Profile/Profile Version，并关联 Agent Instance；
7. [x] Profile 支持乐观版本、审计、回滚、可执行/只读、operation 和 workspace 约束；
8. [x] ActionIntent 自动加载请求 Agent 的 Profile；Profile 只能收紧、不能降低全局审批等级；
9. [x] 移除 Hermes 工具中 Codex/Kimi 的硬编码枚举，Registry 结果显示 Profile 绑定；
10. [x] 完整 unit+contract 回归通过（182 passed）。

## 当前迭代（Iteration 4）

1. [x] migration 006 为 Session Binding 增加 adapter session、capability、recovery 和 replacement 元数据；
2. [x] TaskManager 委派持久化 adapter/native session ID、instance ID、context revision 和 snapshot；
3. [x] 定义 `new/native_resume/replacement/blocked` 恢复决策，replacement 保留前一 binding 审计链；
4. [x] Session turn 使用稳定幂等键，收到成功响应后才单调推进 `last_message_seq`；
5. [x] Adapter 支持显式 terminal replacement；普通消息不能隐式重启已结束 task；
6. [x] TaskManager 支持 pause/resume/interrupt/cancel 远程控制并同步 binding、phase 和审计事件；
7. [x] 本机确认 Codex CLI 0.148.0 的 `exec resume`，实现 JSONL thread ID 捕获和原生恢复 Adapter；
8. [x] 本机确认 Kimi Code 0.37.1 的 `--session`，实现 stream/index session ID 捕获和原生恢复 Adapter；
9. [x] Codex/Kimi 长任务运行中即可暴露 native session ID；interrupt/cancel 不允许晚到结果恢复任务；
10. [x] 完整 unit+contract 回归通过（201 passed）。

真实 Codex/Kimi 模型调用测试仍为显式 opt-in，分别由 `LAS_RUN_CODEX=1`、`LAS_RUN_LLM=1`
开启；本轮只执行 CLI 版本/命令能力探测和离线契约，未消耗外部模型额度。生产发布前必须执行这两项。

## 当前迭代（Iteration 5）

1. [x] 核验本机 DSH 0.1.0-rc.7（最初接入为 rc.6）：`headless` 是不可恢复的一次性新会话，
   生产接入改用 DSH Web 原生 session API；
2. [x] 增加 DSH Agent Card、持久 Session Adapter、8203 独立 A2A 服务、
   gateway 路由/ACL、心跳注册和启动脚本；
3. [x] 增加 DSH Template 与默认只读 Reviewer Profile；仅首次心跳自动绑定，
   已有人工/Profile WebUI 配置不被覆盖。2026-08-20 真实门禁证实 rc.7 会把
   `/permission read-only` 当普通模型 prompt，既不强制权限又与首轮竞态；该
   prompt 已删除。待 DSH 官方 permission API 或可审计专用 preset 接入并实测前，
   控制面 read_only Profile 不能被表述为原生 sandbox；Adapter 已改为默认拒绝
   所有模型 prompt/steer，Card/health 暴露未验证状态，readiness 返回不可用且
   停止在线心跳，Hermes 静态种子标记为 disabled，生产预检阻断 DSH 启用与
   peer 路由；`LAS_PRODUCTION_MODE=true` 还会在运行时拒绝开发豁免，不能靠
   跳过预检绕过。开发豁免仅允许隔离门禁，DSH 整体仍不得生产放行；
4. [x] 支持同一 DSH 原生 session 多轮消息、Adapter 重启恢复、历史/工具事件
   Artifact、interrupt/cancel，并把原生审批挂起映射为 `input-required`；
5. [x] DSH unit+contract 离线测试通过；全量 unit+contract 为 209 passed；
6. [x] 接入 DSH `/api/events.mux` 实时下行与 `/api/respond`，保留稳定
   `rpcId`，把原生 approval/question 的 Hermes/用户决定送回同一 DSH turn；
7. [x] 在 `LAS_RUN_DSH=1` 下验证真实 DSH Web `session.list`（无模型调用）；
   真实双轮与进程重启恢复仍需另行授权模型调用；
8. [x] Hermes 多 Agent/多步骤委派迁移到版本化结构化 Task Plan：每步持久绑定
   Agent/Profile version、依赖、预期操作/产物和验收条件；委派时拒绝 Agent/Profile
   漂移或用户介入后的旧 revision，ActionIntent 超出计划范围升级用户；WebUI 任务
   详情可查看完整计划链。legacy create_task 仅保留给单 Agent 单步骤兼容路径；
9. [x] 为 Codex/Kimi 工具事件建立标准 PendingInteraction/ActionIntent 翻译，
    修改操作执行前必须经过控制面签名回执；
10. [x] 将用户 comment/steer/pause/interrupt/cancel/takeover 语义接入
    TaskManager；WebUI 已开放专用 takeover/return_to_hermes：接管先提升 revision
    并中断原生 turn，控制权保持 user；归还后控制权回 Hermes、进入 needs_replan，
    Hermes 从持久消息读取用户指令，不恢复旧 revision；重复请求不会重复中断，
    无 interrupt 能力或未接管就归还均 fail-closed；本批全量 unit+contract 为
    322 passed；
11. [x] 将 agent session/message/action/tool/artifact 事件统一接入可断线补发的实时 SSE；
12. [ ] 在授权环境执行 Codex/Kimi 真实双轮 resume、进程重启和 Adapter 重启测试；
    Codex 已于 2026-08-20 完成实例重建及随机端口 HTTP Adapter 整进程重启两级
    真实门禁，均以同一 native thread 完成第二轮并复述首轮 marker（各 1 passed）；
    Kimi 仍受服务端 usage limit 阻塞；
13. [x] State Writer 已把 event 去重记录、Task transition、run/artifact 写入合并为
    同一数据库事务，并用“transition/run 首次失败 → 回滚 → 同 event_id 重投成功”
    离线故障注入证明不会因提前 dedupe ACK 丢状态；已增加 gateway 重启后同
    idempotency key 不重复执行，以及 durable consumer/NATS 重启后两条同 event_id
    只落一条 Event/Run 的隔离进程测试。NATS 用例已改为逐测试随机端口，并于
    2026-08-20 在本机真实执行通过（端到端落库与持久存储重启共 2 passed）；
    gateway 用例已改为使用进程内随机 key、随机端口，并把 codex/kimi/dsh 路由
    全部指向临时 fake worker；2026-08-20 真实执行认证、ACL、路由限流、委派和
    gateway 重启幂等共 6 passed。实测同时发现终态 task 重放先于幂等检查的缺陷，
    现已改为同 key 返回原结果、跨 task key 冲突拒绝、失败 revision 不登记 key。
    PostgreSQL 连接故障现会替换 State Writer 的失效连接并继续依赖 JetStream NAK 重投；
    已增加 `LAS_RUN_PG_FAULTS=1` 显式门控的随机 Compose 项目故障测试：停库期间
    验证 durable message NAK，恢复后验证同 event_id 只落一条 Event/Run；该
    PostgreSQL 隔离容器门禁已于 2026-08-20 在本机真实执行通过（1 passed），
    gateway、NATS 与 PostgreSQL 三项隔离故障门禁均已执行通过；本批全量
    unit+contract 为 323 passed。

## 当前迭代（Iteration 6）

1. [x] Session Adapter SDK 新增显式 interaction capability、待处理交互查询、
   权威响应与同一 turn continuation；A2A Task metadata 暴露
   `pendingInteractions`；
2. [x] DSH 使用 `/api/events.mux` SSE 实时接收 `server-request`；WebSocket
   与 SSE 的 DSH 载荷相同，本实现选择更易重连和测试的 HTTP/SSE carrier；
3. [x] 原样保留 DSH 稳定 `rpcId`，审批按 `approvalId`、问题按整批
   `answers[]` 经 `/api/respond` 回到原生 session；
4. [x] `allowed-once` 必须携带已批准、由 user/Hermes 决策的 ActionIntent
   HMAC receipt，并绑定 task/interaction/native rpc/revision；无 receipt、
   签名不匹配、旧/重复 rpcId、未知交互均 fail-closed；
5. [x] migration 007 新增 `agent_session_interactions`，持久化请求、原生
   correlation、ActionIntent 关联、响应者、响应摘要、状态和错误；
6. [x] State Writer 将 `task.input_required` 映射为内部 `blocked`，并在
   Adapter 事件到达时创建交互记录及未知操作默认 `awaiting_user` 的
   ActionIntent；
7. [x] TaskManager 和 Web API 新增 interaction 查询/响应入口，维持
   `user > hermes > agent` 决策层级；任务详情返回完整交互审计链；
8. [x] DSH 原生/A2A/持久层/权限回执测试通过；全量 unit+contract 为
   218 passed；
9. [x] WebUI 增加实时交互卡片、结构化问题控件和逐次批准/拒绝按钮，
   任务详情显示交互审计记录；存在原生交互时禁止用旧任务级审批伪装放行；
10. [x] 以 callId 关联 DSH `ToolEventView`，只保留有界命令/cwd 或变更路径；
    缺少可审查详情时 WebUI 与 Adapter 均只允许拒绝，重连 replay 可从
    history 恢复安全 view；
11. [x] DSH 命令采用 fail-closed 影响面解析：只对无 shell 组合/扩展的明确
    读、测试、文件修改/删除及 Git 操作生成规范化 operation，并把所有目标
    解析为任务工作区内绝对路径；未知、越界、含变量/重定向或已脱敏命令只可
    拒绝。State Writer 在控制面独立重算语义，不信任 Adapter 声明的 operation；
    实时事件、history JSON 与 assistant Artifact 均有界脱敏；修改 approval
    只使用绑定原 RPC 的 `allowed-once`，不开放持久授权。rc.7 原生 read-only
    preset 尚未验证，因此 DSH 修改能力继续作为生产阻塞；本批全量 unit+contract
    为 320 passed；
12. [ ] SSE 断线退避重连、Adapter 恢复后从 history 重建 pending view、控制面
    `/api/respond` 失败后重试，以及“DSH 已接收但响应丢失”按 approval/outcome
    对账且不二次发送，均已有离线故障测试；Adapter/DSH 双端真实进程重启仍待
    授权环境故障注入；已增加 `LAS_RUN_DSH_RESTART=1` 门控的无模型真实测试，
    使用随机端口和临时 `DSH_HOME` 验证原生 session 经 DSH 真实进程重启和
    Adapter 实例重建后仍可 list/history/resume，不触碰用户 `~/.dsh`；该隔离
    门禁已于 2026-08-20 在本机真实执行通过（1 passed），真实 Adapter 服务
    进程重启仍待授权环境执行。2026-08-20 真实模型检查发现并移除伪 permission
    prompt 后，`LAS_RUN_DSH_SERVICE_RESTART=1` 已在随机端口同时重启 DSH Web
    与 HTTP Adapter，并以同一 native session 完成第二轮 marker 恢复（1 passed）。
    该结果覆盖开发豁免下无工具 prompt 的双轮/双进程恢复，不证明原生 read-only
    sandbox；之后已增加代码级默认禁用、Card/health 能力披露及 production-preflight
    阻断。approval 拒绝/允许矩阵必须等官方 permission 接口可验证后再执行；
13. [x] 完成 Codex/Kimi ToolCall/ActionIntent 拦截与用户介入实时 UI，
    再进入生产安全评审。

## 当前迭代（Iteration 7）

1. [x] 核验本机 Codex CLI 0.148.0 与 Kimi Code 0.37.1 的真实非交互能力；
   Codex `exec` 没有 AgentHub 可应答审批通道，但 `codex app-server` 提供
   request/response 形式的逐工具审批；Kimi prompt 模式也不能把审批 correlation
   暴露给平台，禁止把事后 JSONL 解析视为写前门禁；
2. [x] Kimi 生产 Adapter 从 `-p --output-format=stream-json` 迁移到原生
   `kimi acp` JSON-RPC：支持 initialize、session/new、session/load、
   session/prompt、session/cancel 和实时 session/update；
3. [x] 将 ACP `session/request_permission` 映射为统一 PendingInteraction，
   保留 native request/session/toolCall correlation、可审查路径和有界脱敏
   rawInput；原生 turn 在控制面决定前保持挂起；
4. [x] 逐次允许必须携带与 task/interaction/native request/native session/
   context revision 绑定的 ActionIntent HMAC receipt；拒绝映射到 Kimi 提供的
   `reject_once`，允许映射到 `allow_once`，均回应同一 ACP RPC；
5. [x] 使用本机 Kimi 做无模型调用真实 ACP initialize 握手，确认协议版本 1、
   `loadSession=true` 及 resume/cancel 等能力；
6. [x] Codex 生产 Adapter 从 `exec --json` 迁移到原生 `app-server` JSON-RPC；
   新建/恢复 thread 均固定 `read-only + on-request + reviewer=user`，命令、文件
   修改和权限申请保留原 RPC 挂起并映射为 PendingInteraction；只接受绑定
   task/interaction/native request/native thread/context revision 的签名回执，
   允许仅使用 `accept` 或 turn-scope 精确权限，禁止 `acceptForSession` 和
   session-scope 授权；
7. [x] 将 DSH/Kimi/Codex 原生过程通知通过 SessionEvent 统一封装为
   `agent.session.event`，经 NATS 进入 DB 游标事件流；WebUI 全局事件
   和任务详情可实时显示 assistant delta、工具生命周期、计划与原生交互，
   断线后继续由现有 `seq/after` SSE 游标补发；
8. [ ] 执行真实 Kimi approval 挂起/拒绝/允许、双轮 load 和 Adapter 重启故障注入；
   2026-08-20 已尝试真实只读研究任务，ACP 正确返回终态失败，但 Kimi 服务端以
   HTTP 403 拒绝：当前计费周期额度已耗尽。该项是外部配额阻塞，额度恢复或升级
   前不得用重复调用代替验收；
9. [x] 全量 unit+contract 226 passed；集成 9 passed/12 skipped；另行启用
   `LAS_RUN_KIMI_ACP=1` 的真实无模型 ACP session/new 检查 1 passed；本批次
   按自动提交约定记录独立 commit。
10. [x] 实现用户 steer/interrupt Web API 与任务详情操作区：Codex 使用
    `turn/steer(expectedTurnId)`，DSH 使用 `session.prompt(mode=steer)`；Kimi
    ACP 未声明同 turn steer，因此 UI 只提供 interrupt/cancel。任何介入都先写
    conversation message 并原子提升 context revision；当前及重启后的 Hermes
    会在下一轮对话同步 `user.*` 指令，重复 idempotency key 不会二次下发；
11. [ ] 执行 Codex/Kimi 真实 approval 挂起/拒绝/允许、双轮恢复和 Adapter
    重启故障注入；Codex 无模型 `initialize + thread/start` 由
    `LAS_RUN_CODEX_APP_SERVER=1` 单独门控。2026-08-20 已完成 Codex 真实单轮
    修改门禁（1 passed）：原生 interaction 挂起后使用绑定 task/interaction/
    native thread/revision 的签名 receipt 逐次 `allowed-once`，真实创建文件并
    执行 pytest 成功；同日新增 `LAS_RUN_CODEX_RESTART=1` 门禁并真实通过
    （1 passed）：第一 Adapter/App Server 完成 marker turn 后关闭，第二实例以
    同一 native thread 恢复并在第二轮准确复述 marker。该结果覆盖双轮上下文及
    Adapter/App Server 重建，不覆盖 HTTP Adapter 服务进程重启。真实拒绝门禁
    `LAS_RUN_CODEX_REJECT=1` 也已通过（1 passed）：原生修改请求挂起后全部由
    user 拒绝，turn 正常结束且目标文件与任何 `workspace/*` 产物均不存在。
    `LAS_RUN_CODEX_SERVICE_RESTART=1` 随机端口 HTTP 整进程重启门禁亦已通过
    （1 passed），第二服务进程携同一 native thread/context revision 完成第二轮。
    至此 Codex 允许、拒绝、双轮、App Server/Adapter 重建和 HTTP 服务重启矩阵完成。
12. [x] Codex App Server 批次回归：unit+contract 233 passed；集成
    9 passed/13 skipped；本机无模型 `initialize + thread/start` 1 passed。
    同时修正多轮 handle 的 context revision 同步，以及把 Codex App Server/
    Kimi ACP 传输日志排除在“业务文件产物”之外。
13. [x] 实时会话与用户介入批次回归：unit+contract 241 passed；集成
    9 passed/13 skipped；覆盖 A2A same-turn steer、TaskManager revision/
    idempotency、Codex expected turn、DSH 原生 steer、Hermes 上下文同步及
    WebUI 介入 API。

## 2026-08-20 当前发布基线与外部阻塞项

- 当前源码全量 unit+contract：333 passed；默认 integration：10 passed、
  28 skipped。默认跳过项均维持显式门控；gateway、远程安全剖面、NATS、
  PostgreSQL、DSH 无模型重启、HTTPS 告警及备份恢复等已执行的隔离门禁结果，
  以各节记录为准；
- 本机可安全完成的实现与隔离故障注入已收敛。后续不应继续以 mock 或仅健康检查
  替代真实验收，也不应在未授权时调用模型、外部身份系统或发布流水线；
- Codex 真实 approval 允许/拒绝、双轮恢复及 HTTP Adapter 服务重启矩阵已完成；
  DSH 双进程恢复已完成，但生产仍阻塞于官方 permission API/可审计 preset；
- 发布前仍阻塞于 Kimi 真实 approval/双轮/重启矩阵、DSH 原生权限接口、真实
  CA/OIDC 和第二主机 gateway、首次远端供应链
  workflow 与 Cosign/attestation 验证；目标环境正式 HTTPS webhook 失败/恢复；
- Kimi 真实模型门禁当前另受服务端配额阻塞（2026-08-20 HTTP 403 usage limit）；
  额度恢复后从只读单轮门禁重新开始，再执行 approval/双轮/重启矩阵；
- 建议进入 release-candidate 验收阶段，而不是继续扩展功能。取得相应凭据、模型
  调用授权及维护窗口后，按上述四类门禁逐项执行；全部通过后再进行生产部署审批。
