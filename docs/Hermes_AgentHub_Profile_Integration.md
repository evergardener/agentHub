# qishuo Hermes → agentHub 生产接入

状态：2026-08-22 单一 Hub peer 契约。

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
7. 核对终态、产物和审计记录后汇报。

WebUI 审批中心同时列出委派前的 queued/input-required 门禁与 worker 已运行后
产生的 blocked 原生审批；两者使用同一批准/拒绝按钮，但审计事件保留来源。

Kimi 停用时，`agents/list` 会标记 `enabled=false`；如用户仍指定 Kimi，
`tasks/create` 稳定返回 `agent disabled` 与需用户确认的说明，不探测、
不创建任务、不委派、不静默改派。

## 备份与回退 Gate

修改 qishuo 前必须：

1. 创建权限 `0700` 的时间戳备份目录；
2. 复制 `config.yaml`、`.env` 和已有同名 skill，文件权限 `0600`；
3. 生成 SHA-256 manifest，不记录密钥内容；
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
