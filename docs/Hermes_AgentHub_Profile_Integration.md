# qishuo Hermes → agentHub 生产接入

状态：2026-08-24 单一 Hub peer + 持久监督契约。

## 边界

qishuo 只配置一个 `agenthub` A2A peer，不配置 `codex`、`dsh`、
`kimi` 或未来 Agent 的独立路由。完整链路：

```text
qishuo Hermes
  -> agentgateway /agenthub (Hermes identity, only this route)
  -> agentHub Orchestrator + Registry + approval policy
  -> agentgateway /agents/{registry-agent-id} (orchestrator identity)
  -> Registry-resolved Adapter endpoint
```

agentgateway 是 A2A 数据平面，不是 Agent Registry。Registry 依据 Adapter
心跳、租约、人工启停和 Profile 生成发现视图；gateway 负责身份、
ACL、限流、超时、重试和链路观测。新 Agent 正常心跳注册后，qishuo
无需变更配置。

## qishuo profile 配置

只允许在 `~/.hermes/profiles/qishuo` 修改；不得修改全局
`~/.hermes/config.yaml`。目标形状：

```yaml
a2a_agents:
  agenthub:
    url: http://127.0.0.1:8300/agenthub
    auth:
      type: bearer
      token: ${AGENTHUB_A2A_TOKEN}
    timeout: 900
    capabilities: [orchestration, registry, approvals, artifacts]

plugins:
  enabled: [agenthub-supervisor]
  entries:
    agenthub-supervisor:
      allow_gateway_injection: true
```

profile `.env` 仅保存 `AGENTHUB_A2A_TOKEN`，权限必须为 `0600`。token 值与
agentHub `.env` 的 `LAS_HERMES_GATEWAY_API_KEY` 一致。gateway 使用独立的
`LAS_HERMES_BACKEND_TOKEN` 访问 Orchestrator，它才是 `LAS_A2A_PEERS` 中
`{ "peer": "qishuo" }` 的 key，不得写入 qishuo。不在文档、日志或 shell
历史中打印任一 token。

## Hermes 调用契约

qishuo 原生 `a2a_call` 不提供自定义 `metadata.agent`，因此 agentHub
在 A2A `SendMessage` 文本 Part 中使用严格 JSON 控制包。详细示例见
`integrations/hermes-qishuo/agenthub-orchestration/SKILL.md`。流程是：

1. `agents/list`；
2. Hermes 根据用户预设、能力、enabled/online 状态选择 Agent；
3. `tasks/create` 传递未总结的完整 objective；
4. 从可见状态消息保存 `task_id=T-...`（Hermes renderer 可能不展示结构化
   `task.id`）；
5. `input-required` 时向用户请求批准，也可在 WebUI 审批中心处理；
6. 使用同一 `context_id` 调用 `tasks/get/approve/reject`；
7. `agenthub-supervisor` 轮询 agentHub outbox；canonical Gateway route 可持久恢复
   watch，CLI/TUI route 仅在当前 Hermes 进程存活时轮询/注入，发生审批、阻塞、失败
   或等待验收时，以只含 ID/状态的可信 envelope 唤醒可用 surface；
8. Hermes 使用 envelope 中原 `context_id` 调用 `tasks/get`，处理并向用户汇报后
   ACK；未 ACK 会按租约重投，重启后从 profile state 恢复；
9. 核对终态、产物和审计记录后汇报，只有用户显式接受才能 `tasks/accept`。

后台唤醒不授予任何审批权限。`awaiting_user` 必须留给 WebUI 用户，Hermes 只能
处理 `inspectable=true` 且 `action_intent_status=awaiting_hermes` 的交互。Plugin
不得把 objective、worker 内容、工具参数或审批 payload 注入模型上下文；先
`tasks/get` 是强制的权威状态刷新。

WebUI 审批中心同时列出委派前的 queued/input-required 门禁与 worker 已运行后
产生的 blocked 原生审批；两者使用同一批准/拒绝按钮，但审计事件保留来源。

### Hermes Studio 0.6.47 agent bridge 兼容 Gate

Hermes Studio 0.6.47 在初始恢复完成后，如果 Node 侧尚不知道存在 background
delegation，会停止调用 bridge 的 `background_poll`。profile plugin 此后创建的合法
native completion 会停在 Hermes durable queue，不能自动回到原 `mt...` session。
agentHub 插件不能通过伪造 WebUI 状态或改写 session 数据规避这个边界。

在上游提供正式 external-background-activity 信号前，生产机必须安装仓库内的
版本锁定兼容补丁。补丁只接受 npm 发布版 0.6.47 的精确 SHA-256 和精确 runtime
片段，把空闲 IPC poll 调整为每 2 秒一次；原有 claim、session ownership、重试和
ACK 逻辑保持不变。未知版本、未知 hash 或部分补丁全部 fail-closed：

```bash
.venv/bin/python scripts/patch-hermes-studio-agentbridge-poll.py --apply
.venv/bin/python scripts/patch-hermes-studio-agentbridge-poll.py --check
```

安装会输出 byte-for-byte backup 路径。回退时先停止 Hermes Studio，再使用该路径：

```bash
.venv/bin/python scripts/patch-hermes-studio-agentbridge-poll.py \
  --restore /Users/evergarden/.hermes-web-ui/backups/agenthub-agentbridge-poll/\
hermes-web-ui-0.6.47-.../index.js
```

升级 Hermes Studio 前必须先恢复原 runtime；新版本未经重新审计不得强行套用旧
补丁。Hermes Studio 重启后还要在真实 WebUI 会话验证：原 turn 已结束、session
空闲、任务随后完成时，2 秒级自动唤醒、`tasks/get`、汇报和 ACK 均发生。

Kimi 停用时，`agents/list` 会标记 `enabled=false`；如用户仍指定 Kimi，
`tasks/create` 稳定返回 `agent disabled` 与需用户确认的说明，不探测、
不创建任务、不委派、不静默改派。

## 备份与回退 Gate

修改 qishuo 前必须：

1. 创建权限 `0700` 的时间戳备份目录；
2. 复制 `config.yaml`、`.env` 和已有同名 skill，文件权限 `0600`；
3. 同时备份已有 `plugins/agenthub-supervisor`，生成 SHA-256 manifest，不记录
   密钥内容；
4. 在临时目录演练恢复并校验 hash 一致；
5. 只在 Gate 通过后修改 profile；修改后检查 YAML、权限、peer 数量和
   skill 可见性，再重启 qishuo 运行时。

回退时停止 qishuo，用备份恢复三个目标，重新检查 hash/权限并启动。
只回退 qishuo profile，不改全局 Hermes config。

## 生产验收

- qishuo `a2a_list` 只有一个 agentHub 相关 peer：`agenthub`。
- Hermes 首先调用 `agents/list`，能看到 Codex/DSH online、Kimi disabled。
- 用户指定 DSH 的只读任务产生 agentHub task ID，WebUI 可查，DSH Adapter
  日志显示由 gateway 进入；qishuo 历史中不出现 `dsh --profile headless`。
- 写操作先返回 `input-required`，批准前 worker 无调用。
- 用户指定 Kimi 时 Hermes 询问启用或改派，没有 Kimi 心跳探测/任务。
- 对同一 task/context 继续两轮，不生成重复任务。
- qishuo plugin doctor 通过，配置中仅该 profile 对 supervisor 开启
  `allow_gateway_injection`；全局 Hermes 配置未修改。
- Hermes Studio 0.6.47 compatibility check 返回 `state=patched`；未命中精确
  版本/hash 时发布 Gate 失败，不能把 durable queue 入队当作已唤醒。
- Gateway 创建任务后工具结果出现 `agentHub supervision active` 且
  `delivery=gateway-durable`；WebUI agent bridge 创建任务后出现
  `delivery=agent-bridge-durable`，并通过 Hermes 原生 async-completion queue 回到
  原 `mt...` session；普通 CLI/TUI 只能出现 `process-only`，进程退出后不保证
  唤醒。让任务进入批准、阻塞或等待验收时，可用的原 Hermes surface 在一个轮询
  周期内被唤醒并先调用 `tasks/get`。
- 暂停 Hermes 超过一个租约周期后恢复，收到同一个 `notification_id`；ACK 后不再
  重投。Hermes/profile 重启后 Gateway/agent-bridge durable watch 仍存在并继续
  监督；process-only watch 必须重新注册。
- 通知 envelope 不含 objective、worker 回复、tool args 或 approval payload；
  `awaiting_user` 不会被 Hermes 自批，等待验收也不会自动 `tasks/accept`。

安装或升级 profile-local skill/plugin（命令输出只含备份路径，不含 token）：

```bash
.venv/bin/python scripts/install-qishuo-agenthub.py \
  --profile /Users/evergarden/.hermes/profiles/qishuo \
  --agenthub-env .env \
  --skill-source integrations/hermes-qishuo/agenthub-orchestration \
  --prompt-appendix integrations/hermes-qishuo/system-prompt-appendix.md \
  --supervisor-plugin-source integrations/hermes-qishuo/agenthub-supervisor
```

安装器会先创建可校验备份并演练恢复，再更新单一 peer、skill、prompt appendix、
plugin 和 profile-local injection allowlist。完成后运行 plugin doctor 并重启 qishuo
gateway。回退使用安装器输出的备份目录：

```bash
.venv/bin/python scripts/install-qishuo-agenthub.py \
  --profile /Users/evergarden/.hermes/profiles/qishuo \
  --rollback /Users/evergarden/.hermes/profiles/qishuo/backups/agenthub-unified-...
```
