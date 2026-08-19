# 外部总控 A2A 接入契约（orchestrator endpoint）

适用场景：自建 hermes（或其他总控系统）作为顶层规划者，把 agentHub 作为
编排执行平面调用。agentHub 内部 hermes-brain 的规划职责由外部总控接管；
审批策略、产物核验 veto、事件审计等执行侧保障不变。

端点：compose 服务 `orchestrator`，默认 `http://127.0.0.1:8310`
（仅宿主机 loopback）。

## 协议面

```
GET  /.well-known/agent-card.json   编排者卡片（含 supportedInterfaces）
GET  /health                        探活（免鉴权）
POST /a2a                           JSON-RPC 2.0
```

方法一览：

| 方法 | 说明 |
|---|---|
| `SendMessage` | A2A v1.0 提交任务；响应包装 `{"task": ...}` |
| `message/send` | legacy；响应为 bare Task；自然语言审批跟进（deprecated） |
| `tasks/get` | `params.id` → A2A 状态（含 input-required 映射与产物清单） |
| `tasks/approve` | `params.id` → 待批准任务放行并委派（精确动作） |
| `tasks/reject` | `params.id` → 待批准任务取消（精确动作） |

## 鉴权与调用方 identity

除 `/health` 外全部强制鉴权。两种身份：

| 请求头 | 配置 | identity | 路由方式 |
|---|---|---|---|
| `Authorization: Bearer <token>` | `LAS_A2A_PEERS`（JSON：token → {peer, worker}） | peer identity | **固定路由**到映射 worker |
| `X-Agent-Token: <token>` | `LAS_API_TOKEN`（回退 `LAS_ADAPTER_TOKEN`） | legacy identity | `metadata.agent` 指定 |

- 两 header 同时出现且**值不一致 → 401**；同值视为单一身份。
- Bearer token 若恰好等于 `LAS_API_TOKEN`，按 legacy identity 处理。
- 均未配置 = 关闭鉴权（仅本地开发）。

### peer→worker 固定映射（LAS_A2A_PEERS）

```json
{"<token-a>": {"peer": "qishuo-codex", "worker": "codex"},
 "<token-b>": {"peer": "qishuo-kimi",  "worker": "kimi"},
 "<token-c>": {"peer": "qishuo-dsh",   "worker": "dsh"}}
```

- 服务端依据认证 identity 固定选择 worker，**不信任请求体自称的
  `metadata.agent`**；body 中的 `metadata.agent` 与映射冲突 → -32602 拒绝。
- worker 白名单见 `common/config.py ALLOWED_PEER_WORKERS`（当前 codex/kimi/dsh，
  新增 worker 需显式放行）。
- 映射 worker offline/未知 → 稳定 -32602 错误，**不回退**到其他 worker。
- 配置畸形（非法 JSON / 非法项）启动即失败，不静默降级。
- token 只存 `.env`（已 gitignore，chmod 600）并转交对端；不入仓、
  不入日志、不入审计事件（审计只记 peer 逻辑名）。

## SendMessage（A2A v1.0）

```json
{
  "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
  "params": {"message": {
    "role": "user",
    "parts": [{"text": "任务目标", "mediaType": "text/plain"}]
  }}
}
```

- text Part 同时兼容 v1.0 member-presence（`{"text": ...}`）与 legacy
  `{"kind": "text", "text": ...}`。
- peer identity 无需（也不应）传 `metadata.agent`；legacy identity 必须传。
- 响应为 v1.0 SendMessageResponse 包装：`{"task": { ...A2A Task... }}`。
- **不支持自然语言跟进**：带 `metadata.taskId` 的 SendMessage 返回 -32602，
  审批只走 `tasks/approve` / `tasks/reject`。

审批策略（`src/hermes/policy.py`）：

- 只读/查询类（auto）→ 立即委派，状态 `submitted`
- 命中常驻授权（granted）→ 记录 `task.auto_approved` 后委派
- 写操作（ask）→ **不委派**，任务呈现 `input-required`

## tasks/approve | tasks/reject（唯一审批通道）

```json
{"jsonrpc": "2.0", "id": "2", "method": "tasks/approve",
 "params": {"id": "T-20260818-0006"}}
```

- 仅对处于 `input-required`（待批准）的指定 task 生效。
- approve → 记录 `task.approved`（含 peer identity，**不记 token**）并立即
  委派；reject → 记录 `task.rejected`，任务 `canceled`。
- 重复、晚到、终态 task 的操作返回稳定 -32602，**不会重复委派**。
- 未知 task → -32602 `task not found`。

## message/send（legacy，保持兼容）

新任务：`metadata.agent` 必填；响应为 bare Task（无 wrapper）。

审批跟进（deprecated，仅留 legacy client）：带 `metadata.taskId` 时按
**整句精确匹配**「批准/拒绝」放行或取消（2026-08-18 起由子串匹配改为
精确匹配，修复「不批准」被误放行的问题）。新接入方请用
`tasks/approve` / `tasks/reject`。

## tasks/get

```json
{"jsonrpc": "2.0", "id": "3", "method": "tasks/get",
 "params": {"id": "T-20260818-0005"}}
```

返回 A2A Task：

```json
{
  "id": "T-20260818-0005",
  "status": {"state": "completed", "timestamp": "…", "message": "…"},
  "artifacts": [{"name": "workspace/time.txt", "type": "file", "path": "…"}],
  "metadata": {"assigned_to": "kimi", "internal_status": "completed"}
}
```

状态映射（`common/models.py A2A_STATE_MAP`）：内部 created/queued/assigned →
`submitted`；working → `working`；**created/queued + 待批准 →
`input-required`**（message 含风险说明与拟委派 agent）；completed/reviewed/
accepted → `completed`；failed → `failed`；cancelled → `canceled`。

## agent card

```json
{
  "name": "agenthub-orchestrator",
  "version": "0.2.0",
  "url": "http://127.0.0.1:8310",
  "supportedInterfaces": [
    {"url": "http://127.0.0.1:8310", "protocolBinding": "JSONRPC",
     "protocolVersion": "1.0"}
  ],
  "capabilities": {"streaming": false},
  "skills": [...]
}
```

## 轮询建议

长任务安全：send 秒回，结果靠 `tasks/get` 轮询（建议 2–5s 间隔，
`internal_status` 可做细分判断）。终审由外部总控负责——agentHub 的
`artifacts` 清单可用于核验 worker 是否真实产出（防谎报，
见 `tests/unit/test_review_artifact_check.py` 的 veto 语义）。

## 已验证链路

2026-08-17（legacy `message/send` + `X-Agent-Token`）：

| 任务 | 路径 | 结果 |
|---|---|---|
| T-20260817-0022 | A2A → kimi（只读，auto） | completed + analysis.md |
| T-20260817-0023 | A2A → codex（创建文件，命中常驻授权 granted） | completed + workspace/a2a-gate-test.txt |
| T-20260817-0024 | A2A → codex（写操作 ask → input-required → 「批准」放行） | completed + workspace/time.txt |

2026-08-18（v1.0 `SendMessage` + Bearer peer，脚本 `scripts/e2e_phase_a.py`）：

| 验证项 | 结果 |
|---|---|
| 无 token / 错 token / 双 header 冲突 | 401 |
| card supportedInterfaces（JSONRPC / 1.0 / loopback URL） | 通过 |
| SendMessage v1.0 text Part 解析 + `{"task": ...}` 包装 | 通过 |
| peer 固定路由：qishuo-kimi → kimi（T-20260818-0005 completed + analysis.md） | 通过 |
| 伪造 metadata.agent 与映射冲突 | -32602 拒绝 |
| 写任务 → input-required；compat 自然语言审批 | -32602 拒绝 |
| tasks/approve 放行委派；重复 approve | 稳定 -32602，不重复委派 |

（codex peer 任务已确认送达 adapter 并拉起 `codex exec`；当日 codex CLI
本身执行缓慢属 worker 侧环境，与兼容层无关。）

2026-08-18 深夜（kimi worker 切换真实 Kimi Code CLI 后）：

| 任务 | 路径 | 结果 |
|---|---|---|
| T-20260818-0011 | SendMessage（qishuo-kimi peer）→ kimi adapter → `kimi -p`（Kimi Code CLI 0.37.1，OAuth 登录）→ Kimi 模型 | completed + last-message.md，约 5 分钟 |

注：kimi CLI 0.37.1 有两个参数解析怪癖已在 runner 中规避——
`--output-format` 必须用 `=` 形式，且必须放在 `-p` 之前
（`-p` 会把紧随其后的任何 token 吞为 prompt 值）。
