# 外部总控 A2A 接入契约（orchestrator endpoint）

适用场景：自建 hermes（或其他总控系统）作为顶层规划者，把 agentHub 作为
编排执行平面调用。agentHub 内部 hermes-brain 的规划职责由外部总控接管；
审批策略、产物核验 veto、事件审计等执行侧保障不变。

端点：compose 服务 `orchestrator`，默认 `http://127.0.0.1:8310`。

## 协议面

```
GET  /.well-known/agent-card.json   编排者卡片（name: agenthub-orchestrator）
GET  /health                        探活（免鉴权）
POST /a2a                           JSON-RPC 2.0：message/send | tasks/get
```

鉴权：请求头 `X-Agent-Token: <token>`（`LAS_API_TOKEN`，回退
`LAS_ADAPTER_TOKEN`；均空=关闭，仅本地开发）。除 `/health` 外全部强制。

## message/send

### 新任务（无 metadata.taskId）

```json
{
  "jsonrpc": "2.0", "id": "1", "method": "message/send",
  "params": {"message": {
    "role": "user",
    "parts": [{"kind": "text", "text": "任务目标"}],
    "metadata": {"agent": "codex", "project": "可选"}
  }}
}
```

- `metadata.agent` **必填**——派给谁由外部总控规划；agent 必须在线
  （心跳注册，见 `agentctl agent list`），否则 -32602 并附在线列表。
- 审批策略（`src/hermes/policy.py`）：
  - 只读/查询类（auto）→ 立即委派，状态 `submitted`
  - 命中常驻授权（granted）→ 记录 `task.auto_approved` 后委派
  - 写操作（ask）→ **不委派**，任务呈现 `input-required`

### 审批跟进（有 metadata.taskId）

任务处于 `input-required` 时，对同一 taskId 再发消息：

- text 含「批准/同意/放行/通过/approve/ok…」→ 记录 `task.approved`，
  立即委派，状态转 `submitted`
- text 含「拒绝/取消/reject/cancel…」→ 任务 `canceled`
- 其他文本 → -32602 提示可接受的回复

## tasks/get

```json
{"jsonrpc": "2.0", "id": "2", "method": "tasks/get",
 "params": {"id": "T-20260817-0024"}}
```

返回 A2A Task：

```json
{
  "id": "T-20260817-0024",
  "status": {"state": "completed", "timestamp": "…", "message": "…"},
  "artifacts": [{"name": "workspace/time.txt", "type": "file", "path": "…"}],
  "metadata": {"assigned_to": "codex", "internal_status": "completed"}
}
```

状态映射（`common/models.py A2A_STATE_MAP`）：内部 created/queued/assigned →
`submitted`；working → `working`；**created/queued + 待批准 →
`input-required`**（message 含风险说明与拟委派 agent）；completed/reviewed/
accepted → `completed`；failed → `failed`；cancelled → `canceled`。

## 轮询建议

长任务安全：send 秒回，结果靠 `tasks/get` 轮询（建议 2–5s 间隔，
`internal_status` 可做细分判断）。终审由外部总控负责——agentHub 的
`artifacts` 清单可用于核验 worker 是否真实产出（防谎报，
见 `tests/unit/test_review_artifact_check.py` 的 veto 语义）。

## 已验证链路（2026-08-17）

| 任务 | 路径 | 结果 |
|---|---|---|
| T-20260817-0022 | A2A → kimi（只读，auto） | completed + analysis.md |
| T-20260817-0023 | A2A → codex（创建文件，命中常驻授权 granted） | completed + workspace/a2a-gate-test.txt |
| T-20260817-0024 | A2A → codex（写操作 ask → input-required → 「批准」放行） | completed + workspace/time.txt |
