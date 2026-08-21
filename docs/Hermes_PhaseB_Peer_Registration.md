# Hermes Phase B 接入指令 — agentHub A2A peer 注册与联调

> **已废弃（2026-08-21）**：每 worker peer/token 已被单一 `agenthub` peer +
> Registry 动态发现取代。生产请使用 `Hermes_AgentHub_Profile_Integration.md`。

- 状态：ready（agentHub 侧 Phase A 已上线，commit `5ec68f2`）
- 日期：2026-08-18
- 前置文档：`Hermes_Native_A2A_Integration_Handoff.md`（§4/§5 已在 agentHub 侧落地）
- agentHub 契约详情：`docs/orchestrator-a2a.md`

## 0. agentHub 侧已就绪的能力

- 端点 `http://127.0.0.1:8310`（仅 loopback），A2A v1.0 `SendMessage` +
  `{"task": ...}` 响应包装；text Part 兼容 member-presence 形状。
- agent card 含 `supportedInterfaces`（JSONRPC / protocolVersion 1.0）。
- 鉴权 `Authorization: Bearer <peer token>`；peer identity 服务端固定路由
  worker，**不需要也不允许**在 message 里传 `metadata.agent`（传了且与
  映射冲突会被 -32602 拒绝）。
- 审批只走精确动作 `tasks/approve` / `tasks/reject`（params `{"id": "T-..."}`，
  仅对 `input-required` 任务生效，重复/晚到/终态返回稳定 -32602）。
- `tasks/get` 支持轮询（含 `internal_status` 细分状态）。

## 1. 注册两个受限 peer（qishuo `config.yaml`）

token 由用户单独提供（环境引用，不写字面量、不入仓）：

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

约束（承 Handoff §3/§4）：

- 只允许这两个逻辑 peer；endpoint 固定 loopback，禁止 direct-URL 分支。
- 不要启用 `a2a_orchestrate` 对这两个 peer 的 capability fan-out
  （并发/配额/审批策略未单独设计前）。
- 凭据走 qishuo profile 私有 secret 注入。

## 2. 联调验证步骤

1. **发现**：`a2a_discover(agenthub-kimi)` 应读到 card，含
   `supportedInterfaces[0].protocolBinding == "JSONRPC"`。
2. **只读任务**：`a2a_call(agenthub-kimi, "检索 agentHub 项目的 README 要点")`
   → 返回 `{"task": ...}`，`status.state` 为 `submitted`/`working`；
   目标 worker 应为 kimi（可用 `tasks/get` 或用户侧
   `agentctl-host.sh task show <id>` 交叉核对）。
3. **写任务门禁**：`a2a_call(agenthub-codex, "在 ~/AgentWorkspace 写入文件
   hermes-phaseB-smoke.md，内容一行冒烟记录")` → `status.state ==
   "input-required"`，message 含风险说明与拟委派 worker。
4. **审批放行**：对该 task id 调 `tasks/approve` → `submitted`，worker 开始
   执行；再调一次应返回稳定 -32602（不重复委派）。
   - 注意：hermes 现有 native public tools 没有 `tasks/approve`。在受控
     native tool 落地前，可由用户经由 agentHub Web UI
     （http://127.0.0.1:18070）审批中心放行，链路等价。
5. **轮询**：长任务用 `tasks/get` 轮询（建议 2–5s）；native tools 尚无
   `tasks/get` 时按 Handoff §6 Phase C 扩展 `a2a_get_task`，
   不要引入 MCP。

## 3. 验收清单（Handoff §7 Hermes integration acceptance）

- [ ] `a2a_discover` 成功读取 card
- [ ] `a2a_call(agenthub-kimi, <只读>)` 进入 agentHub 且 worker=kimi，拿到结果
- [ ] `a2a_call(agenthub-codex, <写>)` 呈 `input-required`，仅经
      `tasks/approve`（或 Web UI）放行
- [ ] （可选）`a2a_get_task` 与 `agentctl-host.sh task show <id>` 状态一致
- [ ] endpoint 仍仅监听 loopback，未改 Traefik / 端口映射 / Web UI 暴露面

## 4. 已知特性与边界

- codex worker 是真实 `codex exec` CLI，单并发 FIFO，单任务可能 5–15 分钟；
  kimi worker 自 2026-08-18 起同为真实 CLI（Kimi Code CLI `kimi -p`，
  并发 2），实测单任务约 5 分钟。timeout 60s 只覆盖
  send 的秒回，任务结果靠轮询。
- peer token 与 worker 绑定是服务端强制的：用 `agenthub-codex` 的 token
  不可能投递到 kimi，反之亦然。
- legacy `message/send` + 自然语言「批准/拒绝」已 deprecated，不要使用。
- 回滚：从 `a2a_agents` 移除两个 peer 即可；agentHub 侧 token 由用户撤销。
