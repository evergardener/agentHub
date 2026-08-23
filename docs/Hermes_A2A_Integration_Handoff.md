# Hermes ↔ agentHub 直接 A2A 接入开发交接文档

- 状态：**proposed — 开发前需先完成 Phase 0 安全修复**
- 日期：2026-08-18
- 适用对象：qishuo Hermes 作为顶层规划/审查者，agentHub 作为本机任务执行平面
- 依据：[`orchestrator-a2a.md`](orchestrator-a2a.md)、[`Deployment.md`](Deployment.md)、[`adr/0001-hermes-integration-language.md`](adr/0001-hermes-integration-language.md)、`src/orchestrator/a2a_server.py`

## 1. 目标与非目标

### 目标

让 qishuo Hermes 通过 agentHub 现有的 **A2A JSON-RPC endpoint** 向 `codex` / `kimi` worker 提交任务、查询状态、等待完成，并在 agentHub 的服务端审批门禁下处理明确的批准或拒绝。

```text
qishuo Hermes
  └─ stdio MCP adapter（本机工具适配层；不监听网络端口）
       └─ thin local A2A client
            └─ HTTP loopback + X-Agent-Token
                 └─ agentHub orchestrator
                      ├─ policy / task state / event audit / artifact manifest
                      ├─ agentgateway
                      └─ codex / kimi worker adapters
```

### 非目标

本接入不包含：

- 以 MCP 取代 A2A；
- Traefik、域名、LAN 或公网入口；
- 暴露 `webui`、`agentgateway`、worker adapter、PostgreSQL、NATS；
- 直接读写 agentHub PostgreSQL，或绕开 A2A 调用 worker adapter；
- 使用/泄露 `LAS_GATEWAY_API_KEY`；
- 传递任意 JSON-RPC、任意 A2A method、任意 approval 文本；
- phase 1 中的取消、重试、grant 创建/撤销或 artifact 下载。

A2A 是本次 Hermes ↔ agentHub 的**唯一业务集成协议**。Hermes 通过一个新的、本机 stdio MCP adapter 调用该 A2A client：MCP 仅提供工具 schema、受限参数、secret 注入与本地进程边界，不得复制或替代 agentHub 的状态机、审批策略和审计语义。

## 2. 已确认的现状

### Control plane

当前 Compose 栈的 `orchestrator` host exposure 为：

```text
127.0.0.1:8310 → orchestrator:8310
```

容器内部监听 `0.0.0.0:8310`，但 Docker 仅发布到宿主机 loopback。不要错误描述为容器内也 loopback-only。

当前协议端点：

| Endpoint | 当前行为 |
|---|---|
| `GET /health` | 免鉴权，返回 liveness；不证明 token 有效或可提交任务。 |
| `GET /.well-known/agent-card.json` | token 配置时要求 `X-Agent-Token`。 |
| `POST /a2a` | token 配置时要求 `X-Agent-Token`；仅支持 `message/send`、`tasks/get`。 |

上游 token 解析为 `LAS_API_TOKEN`，缺失时兼容回退 `LAS_ADAPTER_TOKEN`；两者都为空时上游可无鉴权运行。这只是开发兼容行为，**qishuo integration 必须要求专属且非空的 `LAS_API_TOKEN`，并 fail closed**。

### 当前 A2A 语义

- 新任务：`message/send`，`metadata.agent` 必填。
- 状态查询：`tasks/get`。
- worker dispatch 是异步的；提交请求成功不代表任务已经完成，后续以 `tasks/get` 为准。
- 常见公开状态：`submitted`、`working`、`input-required`、`completed`、`failed`、`canceled`。
- `input-required` 不是终态，代表服务端审批策略要求显式跟进。
- 当前 `internal_status` 仅用于诊断展示，不是对 qishuo 承诺的命令接口。
- 历史验收记录显示 codex/kimi 链路曾通过；不得将历史记录表述为当前运行保证，实时可用性必须在接入时检查。

### 现有缺口（必须如实保留）

1. 当前 orchestrator 允许任意已注册且在线的 agent；它**未**在服务端限制为 `codex,kimi`。
2. 当前 A2A approval follow-up 使用自然语言子串匹配，且 approve 判断先于 reject 判断；例如 `不批准` 中包含 `批准`，可能造成错误授权。
3. 当前 orchestrator A2A endpoint 未消费入站 `metadata.idempotencyKey` 或 `metadata.traceId`；网络结果不确定时不能安全重试 `message/send`。
4. endpoint 返回 artifact manifest 的 `name`、`type`、`path`，但不返回存储的 `sha256`；不可宣传为 endpoint 已完成文件 hash/content 验证。
5. 当前没有显式 `tasks/approve` 或 `tasks/reject` RPC method。

## 3. 信任边界与密钥设计

### qishuo 私有配置

建议使用以下专属配置文件：

以下文件名为现有安装兼容标识，不随仓库目录改名：

```text
/Users/evergarden/.hermes/profiles/qishuo/secrets/local-agent-system.env
```

权限要求：

```text
owner: evergarden
mode: 0600
```

字段：

```dotenv
LAS_ORCHESTRATOR_URL=http://127.0.0.1:8310
LAS_API_TOKEN=<locally managed secret>
```

规则：

- token 由本机安全来源填入，禁止通过聊天、代码库、日志、CLI 参数或文档传递；
- client 运行时读取环境变量，不将 token 写入任何缓存、错误、任务文本或 artifact；
- 缺失、空值、权限过宽、非普通文件（含不接受的 symlink 策略）时，bridge 必须拒绝启动或拒绝受保护操作；
- client 必须仅接受固定 endpoint：`http://127.0.0.1:8310`；拒绝非 loopback URL、重定向、代理和用户传入的 destination；
- `LAS_GATEWAY_API_KEY` 与 `LAS_ADAPTER_TOKEN` 不作为 qishuo 对 orchestrator 的凭据来源。

### 请求保护

- `httpx` 需设置有限的 connect/read/write/pool timeouts；
- 禁用 redirect follow；
- 对提交和 approve/reject 禁止自动重试，避免产生重复副作用；
- 错误输出必须递归脱敏 `LAS_API_TOKEN`、`X-Agent-Token`、`Authorization`、以及常见 `token/key/secret/password=value` 形式；
- 禁止在异常中输出 request、response、headers、client `repr` 或完整环境。

## 4. 推荐的 qishuo MCP adapter 与 A2A client interface

实现为一个本机私有的 **stdio MCP server**：它向 Hermes 暴露结构化 tools，内部调用薄 A2A client。它不需要新的网络 listener，也不需要改变 agentHub Compose。

推荐项目归属：新增、版本化并测试 `src/tools/orchestrator_server.py`；以项目 `.venv/bin/python -m tools.orchestrator_server` 运行，由 qishuo MCP registry 启动。qishuo profile 仅持有 MCP registration 和其 `0600` token file。

### MCP tools / 允许的高层操作

```text
agenthub_health()
agenthub_submit_task(objective, agent, project=None)
agenthub_get_task(task_id)
agenthub_wait_task(task_id, timeout_seconds, poll_interval_seconds)
agenthub_respond_to_approval(task_id, decision=approve|reject)
```

每一个 tool 都映射为下述受限 A2A client operation：

```text
health()
submit(objective, agent, project=None)
get(task_id)
wait(task_id, timeout_seconds, poll_interval_seconds)
approve(task_id)
reject(task_id)
```

### 约束

| Operation | 约束 |
|---|---|
| `submit` | `agent` 只能是精确小写 `codex` 或 `kimi`；objective 必须非空；返回 A2A 初始 Task 原意，不自动处理 `input-required`。 |
| `get` | 仅调用 `tasks/get`；不解释或访问 artifact path。 |
| `wait` | 仅轮询 `tasks/get`；默认 2–5 秒 interval；限制最大总等待时间（建议 600 秒）；终态为 `completed/failed/canceled`，并防御性处理 `rejected`；超时返回 task ID + last-known status，不能重新提交。 |
| `approve/reject` | **仅在 Phase 0 服务端修复后开放**；调用方只能传递枚举动作，不能传递自由文本。 |
| `health` | 仅 liveness 检查；不可当作鉴权或 worker readiness 成功信号。 |

不允许的操作：raw JSON-RPC passthrough、任意 agent 名、任意 URL、任意 headers、direct DB、direct adapter、gateway key、retry/cancel/grant management。

### Agent allowlist

qishuo client 必须在网络请求前 enforce：

```text
ALLOWED_AGENTS = {"codex", "kimi"}
```

服务端也应新增 caller/agent scope enforcement，避免其他 client 绕过 qishuo allowlist。在线状态不等于授权状态。

## 5. Wire contract

### 5.1 健康检查

```http
GET /health
Host: 127.0.0.1:8310
```

预期：HTTP `200`，例如：

```json
{"status":"ok","agent":"orchestrator"}
```

### 5.2 提交任务

```http
POST /a2a
Content-Type: application/json
X-Agent-Token: <LAS_API_TOKEN>
```

```json
{
  "jsonrpc": "2.0",
  "id": "client-generated-request-id",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "任务目标"}],
      "metadata": {
        "agent": "codex",
        "project": "optional-project-slug"
      }
    }
  }
}
```

注意：在服务端真正实现入站 idempotency 前，**不要**在文档或 client 中承诺 `idempotencyKey` 可以防重。若收到超时、连接中断或不完整响应，调用方必须把结果标为 ambiguous，并先 `tasks/get` / operator inspection，而不是盲目重发。

### 5.3 查询任务

```json
{
  "jsonrpc": "2.0",
  "id": "client-generated-request-id",
  "method": "tasks/get",
  "params": {"id": "T-YYYYMMDD-NNNN"}
}
```

响应中的 artifacts 仅作为 manifest；若未来需要真实性验证，必须设计一条授权的、独立的 artifact lookup/hash verification 路径，并为该 endpoint 增加专属测试。

## 6. Phase 0：先修复 approval protocol（阻塞项）

### 问题

当前服务端对 follow-up 文本进行 substring 判断，并先检查 approve，再检查 reject。此行为不可作为高风险审批接口：含 `批准`、`ok`、`可以`、`执行` 等子串的自然语言可能意外授权；含批准和拒绝词的混合文本也会有顺序依赖。

### 必须替换为精确动作协议

推荐新增明确 RPC methods：

```text
POST /a2a
method: tasks/approve
params: {"id": "T-..."}

POST /a2a
method: tasks/reject
params: {"id": "T-..."}
```

可选替代：保留 `message/send` follow-up，但必须有严格 schema：

```json
{
  "metadata": {
    "taskId": "T-...",
    "action": "approve"
  }
}
```

无论选哪一种，要求如下：

1. 只接受精确规范化的 enum `approve` 或 `reject`；
2. 缺少 action、空白、未知值、混合值、额外冲突动作一律返回 `-32602`；
3. 禁止从文本 parts 推断审批动作；
4. action 必须只作用于一个处于 `input-required` / pending approval 的指定 task ID；
5. duplicate / late approve 或 reject 的响应必须明确、稳定并可测试；
6. server-side event 必须记录 actor、task ID、decision、timestamp，不能记录 token；
7. qishuo client 在 server-side exact action contract 上线前，不提供批准/拒绝操作。

### 必须加入的回归测试

- exact `approve`：只委派一次；
- exact `reject`：只取消，不委派；
- `不批准`、`approve then reject`、`批准后拒绝`、`ok`、空白、缺少 action、未知 enum：均不得授权；
- 重复、过期、终态 task 的 approve/reject 不得改变历史或二次委派；
- 写任务初始返回 `input-required` 时，确认没有 worker dispatch；
- 只读/服务端 grant 命中场景仍保留既有 policy 行为。

## 7. 开发阶段与验收门

### Phase 1：服务端协议与授权边界

1. 完成 Phase 0 exact approval action 改造和单元/contract tests。
2. 在 orchestrator 服务端增加 client/agent scope：本 qishuo integration 至少只允许 `codex,kimi`。
3. 明确 caller identity 方案。仅共享 API token 无法区分不同 client；如需要真正的 caller-specific allowlist，应使用独立 qishuo token 或令牌 metadata/claims，而非客户端自称。
4. 明确任务提交的未知结果处理语义；若需 client retry，先实现 server-side inbound idempotency keyed by caller + idempotency key。

**Gate：** Phase 0 approval regression、agent authorization、auth-failure tests 全部通过，才允许接入 qishuo approve/reject。

### Phase 2：qishuo thin A2A client

1. 实现项目内 stdio MCP adapter 和其私有 A2A client，固定 endpoint / strict secret-file validation / token redaction。
2. MCP adapter 仅注册 `agenthub_health`、`agenthub_submit_task`、`agenthub_get_task`、`agenthub_wait_task`；对应实现 `health`、`submit`、`get`、bounded `wait`。
3. 只有在 Phase 1 通过后才注册 `agenthub_respond_to_approval`，并仅接受 `approve|reject`。
4. adapter/client 不拥有任务状态、不解析 artifact path、不调用 PostgreSQL。
5. adapter 的 stdout 专用于 MCP stdio protocol；运行日志只记录非敏感 request ID、task ID、method、HTTP/result category、duration。

**Gate：** mock transport tests 覆盖 auth header、非 loopback rejection、无 redirect、allowlist、非空 objective、timeout、transport failure、JSON-RPC errors、token redaction、无 auto-retry。

### Phase 3：本机 live acceptance

按以下顺序，且每一步都记录 task ID：

1. `health`：确认 orchestrator liveness。
2. 使用真实 token 获取 agent card，确认 protected route 可用。
3. 只读 Kimi task：submit → bounded wait → get；确认结果与 `agentctl-host.sh task show <id>` 一致。
4. 安全的 Codex 写任务：必须先返回 `input-required`，确认未委派；随后明确 approve；wait 至终态。
5. 核对 agentHub task、事件和 artifact manifest；不要把 manifest path 当作已验证文件内容。
6. 测试错误 token、offline worker、无效 agent、无效 action、wait timeout；确保不产生额外 task 或二次提交。

**Gate：** task ID、target worker、approval event、终态、artifact manifest 在 qishuo client、A2A response 和 agentctl/Web UI 中一致。

### Phase 4：长期运行与回滚

- 本期不新增 cron、webhook 或对外通知；先收集本地 client 的失败类别与调用频率。
- 出现可用性/鉴权异常时，disable qishuo bridge；不要以 direct adapter 或 DB access 旁路。
- 需要扩展 retry/cancel、artifact verification、dynamic agent routing 或 remote dashboard 时，走独立 ADR 和变更审查。

## 8. 测试矩阵

| 范畴 | 最低验收 |
|---|---|
| Endpoint/auth | 无 token、错误 token → 401；正确 token → card/A2A 可用；`/health` 的免鉴权语义明确记录。 |
| Secret file | 缺失、空值、权限过宽、格式错误、不可接受文件类型 → client fail closed；任何输出不含 token。 |
| Target boundary | 只接受 `http://127.0.0.1:8310`；拒绝 LAN/public host、其他 port、HTTPS downgrade/redirect、代理。 |
| Agent scope | 精确 `codex/kimi` 可提交；`fake`、空格、大小写变体和任意注册 ID 在发请求前被拒绝。 |
| Submit | 只读任务只创建一次并返回 initial task；`input-required` 原样返回且不会自动 follow-up。 |
| Approval | exact enum 正确工作；所有模糊/自然语言/混合词不授权；重复或延迟 action 稳定处理。 |
| Polling | `working → completed`、`failed`、`canceled`、`input-required`、timeout 都有确定行为；timeout 不重新提交。 |
| Failure handling | HTTP 401、connect/read timeout、invalid JSON、JSON-RPC `-32601/-32602` 可定位且不泄露 secret。 |
| Artifacts | 仅验证 manifest 结构；任何 hash/content verification 主张必须由后续专用 endpoint + tests 支撑。 |

## 9. 对 qishuo 的交互呈现要求

qishuo 应向 master 呈现：

- submitted：task ID、目标 agent、任务目标摘要；
- input-required：task ID、agentHub 返回的风险/审批说明、明确的 approve/reject 选择；
- waiting：当前状态和已等待时间，不隐藏 timeout；
- terminal：状态、task summary、artifact manifest、验证范围；
- failure：HTTP 或 JSON-RPC 类别、task ID（如已有）、下一步诊断建议，且无 secret。

qishuo 不应：

- 将 A2A `submitted` 表述为任务已完成；
- 自动批准任何 `input-required`；
- 将 artifacts 表述为内容/哈希已验证；
- 通过自然语言猜测批准意图；
- 在不确定提交结果时自动重试创建任务。

## 10. 回滚与停用

1. 禁用/移除 qishuo local A2A bridge；
2. 删除 qishuo 私有 token 文件或撤销其独立 token；
3. 保持 agentHub Docker Compose、worker、database 与 task history 不变；
4. 不使用 direct worker adapter、agentgateway 或 PostgreSQL 作为恢复性替代路径；
5. 若 token 曾暴露，立即仅旋转 orchestrator audience 的 token，并重新验证 protected A2A card + submit access。

## 11. 实施前 checklist

- [ ] 先完成 Phase 0 exact approval parser/method remediation。
- [ ] server-side agent authorization 和 qishuo client allowlist 均限制为 `codex,kimi`。
- [ ] 明确独立 qishuo caller token 或等价 caller identity，而不是复用无 scope 的共享 token。
- [ ] A2A client 对 missing/unsafe secret、non-loopback target、redirect、timeout fail closed。
- [ ] A2A server 支持的实际方法、artifact 字段和 idempotency 行为均与文档一致。
- [ ] mock/contract/live tests 分层完成；写任务 live acceptance 另行经过明确批准。
- [ ] 在验证完成前，不注册 production qishuo automation，不修改 Traefik 或开放网络入口。

## 12. 参考文件

```text
docs/orchestrator-a2a.md
src/orchestrator/a2a_server.py
src/orchestrator/task_manager.py
src/orchestrator/state_store.py
src/hermes/policy.py
tests/unit/test_orch_a2a.py
tests/contract/test_a2a_contract.py
docker-compose.yml
```
