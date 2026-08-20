# agentHub 生产接入与部署文档

适用版本：Evolution v3（M1–M4 + 加固轮，commit `c8c99de` 起）
目标读者：在新机器上部署 agentHub 控制面并接入 worker agent 的运维者。

---

## 1. 架构拓扑

```
┌─────────────────────── Docker（控制面，compose 管理）───────────────────────┐
│  nats:4222   postgres:5432   state-writer   janitor                        │
│  agentgateway:8300   webui:18070   jaeger:16686   agentctl（按需 run）      │
└─────────────────────────────────────────────────────────────────────────────┘
        ▲ 心跳注册（NATS）          ▲ A2A 委派（经 gateway → host.docker.internal）
        │                           │
┌───────┴───────────────────────────┴────────── 宿主机（worker）─────────────┐
│  codex :8201   kimi :8202   dsh :8203（launchd 常驻，token 鉴权）          │
│  Codex/Kimi CLI、DSH Web :3080、LLM 端点（127.0.0.1:8317）用户自装         │
└─────────────────────────────────────────────────────────────────────────────┘
```

核心原则：
- worker agent **不进容器**——用宿主机原生环境与授权，经心跳自注册，没注册就不可用
- 密钥只走环境变量 / `.env`（权限 600），不入库、不入仓、不用 Keychain
- 状态唯一事实源是 PostgreSQL（可选 SQLite）；NATS 只是事件总线

## 2. 前置条件

| 依赖 | 要求 |
|---|---|
| Docker + compose 插件 | Docker Desktop（macOS）或 Docker Engine 24+（Linux） |
| LLM 端点 | OpenAI 兼容接口（本项目用本地 cliproxy `127.0.0.1:8317`） |
| worker runtime | 按需自装：Codex CLI、Kimi Code CLI，以及 DSH；DSH Adapter 要求先运行 `dsh web --host 127.0.0.1 --port 3080` |
| 网络 | 容器可回连宿主机（compose 已配 `host.docker.internal:host-gateway`） |

## 3. 部署步骤

### 3.1 获取代码 / 镜像

```bash
git clone git@github.com:evergardener/agentHub.git
cd agentHub   # 下文统称项目根
```

镜像两种来源：
- **本地构建**（默认）：`docker compose build`；Docker Hub / PyPI 不可达时：
  ```bash
  docker compose build \
    --build-arg REGISTRY=docker.m.daocloud.io/library \
    --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
  ```
- **ghcr 拉取**：`ghcr.io/evergardener/agenthub:latest`（或 `v*` tag），
  把 compose 里 `agenthub:latest` 锚点的 `build: .` 去掉、改为该镜像名。

基础镜像和 Compose 外部服务均同时固定可读 tag 与 OCI digest。`REGISTRY`
只能指向保持上游 manifest digest 不变的 pull-through cache；镜像升级通过
Dependabot PR 或人工 PR 更新 tag + digest，禁止仅改 tag 后直接部署。

### 3.1.1 镜像供应链验收

`main`/`v*` 的镜像工作流执行以下门禁：

1. 所有第三方 GitHub Actions 固定到完整 commit SHA；
2. BuildKit 先把多架构镜像作为 `candidate-<commit>` 推送，并附加 max-mode
   provenance 与 SBOM attestation；
3. Trivy 扫描候选的不可变 digest，存在已有修复的 HIGH/CRITICAL 漏洞即失败；
4. 扫描通过后，Cosign 使用 GitHub OIDC 对同一 digest 做 keyless 签名；
5. 只有已扫描、已签名的 digest 才晋升为 `latest` / `sha-*` / semver 正式标签。

验证已发布镜像（把 digest 换为工作流输出）：

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/evergardener/agentHub/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/evergardener/agenthub@sha256:<digest>
docker buildx imagetools inspect ghcr.io/evergardener/agenthub@sha256:<digest>
```

### 3.2 配置 .env

```bash
cp .env.example .env && chmod 600 .env
```

必填项（其余见 .env.example 注释）：

| 变量 | 说明 |
|---|---|
| `LAS_LLM_BASE_URL` | **宿主机视角**的 LLM 地址（如 `http://127.0.0.1:8317/v1`）；容器侧由 compose 固定改写为 `host.docker.internal`，不要在本文件填容器地址 |
| `LAS_LLM_API_KEY` | LLM 端点密钥 |
| `LAS_LLM_MODEL` | 模型名（如 `deepseek-ai/DeepSeek-V4-Flash`） |
| `LAS_GATEWAY_API_KEY` | gateway 认证 key，`openssl rand -hex 24` 生成；**留空 gateway 拒绝所有请求** |
| `LAS_PG_PASSWORD` | PostgreSQL 密码；**已有数据卷时改它会导致认证失败**（见 §6.2） |
| `LAS_ADAPTER_TOKEN` | 留空即可——worker 首启自动生成随机值回写本文件 |
| `LAS_ACTION_RECEIPT_SECRET` | ActionIntent receipt HMAC 密钥；生产用 `openssl rand -hex 32` 独立生成；暂时可回退 adapter token |
| `LAS_API_TOKEN` / `LAS_A2A_PEERS` | 外部 Hermes A2A 身份；至少配置一项（或由 API token 回退到 adapter token），compose 无认证会拒绝启动 |
| `LAS_WEBUI_TOKENS` | WebUI 登录 token→role JSON；token 用 `openssl rand -hex 24` 生成，role 为 `viewer` / `operator` / `admin` |
| `LAS_WEBUI_SESSION_SECRET` | WebUI 签名 session cookie 的独立 HMAC 密钥，使用 `openssl rand -hex 32`；未配置时 WebUI 拒绝启动 |
| `LAS_ADAPTER_BIND` | worker 监听地址，默认 `127.0.0.1`；需容器回连时加宿主机 LAN IP |
| `LAS_DSH_PERMISSION_PRESET` | 必须为 `read-only`；修改仅允许经语义 target normalization、ActionIntent 签名回执绑定原 RPC 的 `allowed-once`，不开放持久 `workspace-write` |
| `LAS_DATABASE_URL` | 留空 = compose PG；`sqlite:////path/x.db` = SQLite；外部 PG 直接填 URL |
| `LAS_OTEL_ENDPOINT` | compose 内已指向 jaeger；置空关闭 tracing |

填写完成后运行生产预检。它只输出变量名和修复建议，不输出任何密钥值：

```bash
python3 scripts/production-preflight.py .env
# HTTPS 反代部署使用严格模式（loopback HTTP 的 cookie warning 也视为失败）
python3 scripts/production-preflight.py --strict .env
```

只有显示 `PASS`（或明确接受非 strict 的 loopback cookie / WebUI-only 告警
warning）后再启动；正式生产建议使用 `--strict` 并配置 HTTPS webhook。

上表是默认同机 loopback 剖面。跨主机 gateway 不使用长期 API key，改为 TLS
1.3/mTLS + strict JWT，并由 `LAS_GATEWAY_JWT_FILE`、CA、client cert/key 文件
供 Hermes 使用；服务端独立 Compose 与轮换流程见 `docs/agentgateway.md`。

### 3.3 启动控制面

```bash
docker compose up -d
docker compose ps          # 全部 Up / healthy
```

`state-writer` 会同时探测数据库与 NATS，`janitor`/`notifier` 探测数据库，WebUI 和
Orchestrator 的 `/ready` 会验证数据库，agentgateway 则验证监听端口。依赖服务
未 ready 时，下游不会提前启动；连续失败会在 `docker compose ps` 显示
`unhealthy`。所有容器使用有界 `json-file` 日志（单文件 10MB、保留 5 个），
并配置 CPU、内存和 PID 上限，避免单一控制面组件拖垮宿主机。

入口：
- Web UI（看板/审批/事件流/复审记录）：http://127.0.0.1:18070；首次打开输入 `.env` 中某个 `LAS_WEBUI_TOKENS` key
- Jaeger：http://127.0.0.1:16686
- **外部总控 A2A 端点**（自建 hermes 接入）：http://127.0.0.1:8310，
  契约见 [docs/orchestrator-a2a.md](orchestrator-a2a.md)。支持 A2A v1.0
  `SendMessage`（Bearer peer token，`LAS_A2A_PEERS` 配置 peer→worker
  固定映射）与 legacy `message/send`（`X-Agent-Token`，`LAS_API_TOKEN`
  回退 `LAS_ADAPTER_TOKEN`）；审批走 `tasks/approve` / `tasks/reject`
- 与 hermes 对话（二选一）：
  - 容器模式：`docker compose run --rm agentctl chat`
  - **宿主机直连**：`./scripts/agentctl-host.sh chat`——hermes 就是本仓库的
    Python 模块，不必须在容器里跑；基础设施已映射到 127.0.0.1，包装脚本
    自动把 .env 翻译为宿主机视角（PG/gateway 地址改写）。前提是宿主机
    已 `pip install -e .`（注册 `agentctl` 入口点）。

### 3.3.1 告警与通知

Janitor 的租约过期、执行超时、产物缺失，以及重试耗尽的任务会写入持久
`alerts` outbox，并产生 `system.alert` 审计事件。WebUI「告警中心」实时显示未确认
告警；`operator`/`admin` 可确认，`viewer` 只读。同一 kind/task/detail 只建立一条
告警，重复发生增加 `occurrences`，不会形成通知风暴。

外部通知是显式启用的：

```dotenv
LAS_ALERT_WEBHOOK_URL=https://alerts.example.com/agenthub
LAS_ALERT_WEBHOOK_TOKEN=<至少16字符的Bearer token>
LAS_ALERT_POLL_INTERVAL=10
LAS_ALERT_WEBHOOK_TIMEOUT=10
```

非 loopback webhook 必须使用 HTTPS，URL 不允许内嵌账号密码。投递只发送告警
字段，不发送任何 Agent 隐藏推理或系统密钥；HTTP/传输失败采用 5 秒起、最长
5 分钟的指数退避，连续三次失败把原告警升级为 `critical`。未配置 webhook 时
告警仍完整保留在数据库/WebUI，生产预检给出 warning，`--strict` 会阻止上线。
Webhook 恢复后 notifier 继续投递未成功记录，成功告警不会重复发送。

本机隔离 HTTPS 故障门禁需显式开启：

```bash
LAS_RUN_ALERT_WEBHOOK=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_alert_webhook_fault.py
```

该门禁使用临时自签证书、随机 loopback 端口和临时 SQLite：连续三次 503 后确认
原告警升级为 `critical`，切换 204 后确认恢复投递且不重复发送。2026-08-20 已在
本机真实执行通过（1 passed）。这不替代目标环境正式证书、反向代理、DNS、网络
策略和接收端的同类演练。

### 3.4 接入 worker（宿主机）

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # worker 运行依赖

./scripts/agent-worker.sh codex                # 手动前台启动（调试用）
./scripts/install-agent-autostart.sh codex     # launchd 常驻（macOS）
./scripts/install-agent-autostart.sh kimi

# DSH 仍可独立使用；Adapter 连接其 loopback Web API
dsh web --host 127.0.0.1 --port 3080
./scripts/install-agent-autostart.sh dsh
```

验证：

```bash
docker compose run --rm agentctl agent list    # codex/kimi/dsh 应为 online
curl -H "X-Agent-Token: $(grep ^LAS_ADAPTER_TOKEN= .env | cut -d= -f2)" \
     http://127.0.0.1:8201/health              # {"status":"ok",...}
```

Linux 宿主机没有 launchd：用 systemd user unit 或 `nohup scripts/agent-worker.sh codex &` 代替（`agent-worker.sh` 与平台无关）。

### 3.5 冒烟验收

```bash
docker compose run --rm agentctl chat
# you> 请委派 kimi 分析 XXX        → 研究类自动批准
# you> 请委派 codex 创建文件 …     → 首次会问审批，批准后即执行
```

验收点：任务终态 `accepted`；`artifacts` 含产出文件；Web UI 任务详情可见复审记录；Jaeger 有完整 trace。

### 3.6 DSH/Kimi/Codex 原生交互闭环

DSH 产生 approval/question 后，任务进入 `blocked`，WebUI「AGENT 交互」卡片
实时显示请求。审批只能选择本次允许或拒绝；本次允许会先由控制面决策关联的
ActionIntent，再把不可伪造的 receipt 送到 DSH `/api/respond`。问题以 DSH
定义的 question id 和选项结构整批回答，二者均继续原 native session/turn。
此时旧的任务级 approve/reject 会明确拒绝，避免只修改数据库状态却没有回应
原生 Runtime。

2026-08-20 已使用本机现有 DSH 配置短暂启动 loopback Web，完成真实只读模型
门禁：Adapter 固定 `read-only`，prompt 明确禁止工具调用，turn 最终 completed，
原生 session ID 可追溯并生成 2 个有界产物；随后已停止临时 Web 进程。该结果
不覆盖 approval 拒绝/允许、双轮恢复或 Adapter 服务进程重启。

Kimi Adapter 使用 `kimi acp`，ACP 的 `session/request_permission` 走相同
`blocked -> ActionIntent -> signed receipt -> native response` 链。允许仅选择
Kimi 本次请求提供的 `allow_once`，拒绝选择 `reject_once`；prompt CLI 的
stream-json 日志只可作为旧 Artifact，不得作为生产审批门禁。可先用以下命令
做不调用模型的协议握手检查：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientInfo":{"name":"agenthub-probe","version":"0.1.0"},"clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false},"terminal":false}}}' \
  | kimi acp
```

2026-08-20 真实 Kimi 只读研究门禁被服务端 HTTP 403 阻塞：当前计费周期 usage
limit 已耗尽。ACP/Adapter 能把错误转换为可追溯的任务终态，但这不算模型验收
通过；额度恢复或升级前不要自动重试，恢复后重新运行
`LAS_RUN_LLM=1 ... test_kimi_research_task`。

Codex Adapter 使用 `codex app-server --stdio`。所有 thread（包括恢复的旧
thread）均由 Adapter 覆盖为 `read-only`、`approvalPolicy=on-request`、
`approvalsReviewer=user`。命令、文件变更和附加权限请求在原生 JSON-RPC 上
保持挂起；控制面批准 ActionIntent 后才以签名 receipt 回答同一 request。
Adapter 只发送一次性 `accept`，权限请求只授予原请求且 scope 固定为 `turn`，
绝不发送 `acceptForSession` 或 session-scope 权限。无模型协议检查：

```bash
LAS_RUN_CODEX_APP_SERVER=1 \
  .venv/bin/python -m pytest -q tests/integration/test_codex_app_server.py
```

真实模型与逐次审批门禁需显式授权后运行：

```bash
LAS_RUN_CODEX=1 \
  .venv/bin/python -m pytest -q tests/integration/test_codex_adapter.py
LAS_RUN_CODEX_RESTART=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_codex_restart_fault.py
```

2026-08-20 已在 pytest 临时工作区真实执行通过（1 passed）：Codex 的修改请求
保持挂起，测试以绑定当前原生 request/thread/context revision 的签名 receipt
逐次 `allowed-once`，随后验证真实源码产物与 pytest。该用例不覆盖拒绝、双轮
恢复或 Adapter 服务进程重启。2026-08-20 第二条门禁也已真实通过（1 passed）：
关闭第一 Adapter/App Server 后，新实例以相同 native thread 完成第二轮并准确
复述第一轮 marker；它仍不替代 HTTP Adapter 服务进程重启测试。

三类 Adapter 的原生过程事件统一发布为 `agent.session.event`，WebUI 的全局事件
区与任务详情「事件时间线（实时）」会显示 `nativeEventType` 和脱敏摘要。浏览器
断线重连时携带最后 `seq`，由 `/api/events/stream?after=...` 补发；NATS 短暂
不可用时 Adapter 先写 `events-pending.jsonl`，恢复后按既有 replay 流程回放。

任务详情中的「用户介入」支持备注、实时纠正、中断、接管、归还 Hermes 和取消。
实时纠正按钮只在 Session capability `steer=true` 时显示：Codex/DSH 当前支持，
Kimi ACP 当前不支持。接管仅在 `interrupt=true` 时开放：平台先提升 revision，
再中断原生 turn 并把 controller 保持为 user；归还后 controller 切回 Hermes、
phase 进入 `needs_replan`，旧 turn 不会恢复。所有操作会写入 conversation message
与 `user.intervened` 审计事件；刷新或重启 Hermes 后仍可恢复。若 Kimi 指令
偏差，应先点“中断当前 turn”，再由 Hermes 按新 revision 下发下一轮，不得把
新 prompt 伪装成对旧 turn 的 steer。

以下 `curl` 示例先登录并保存 cookie/CSRF（浏览器会自动处理）：

```bash
curl -sS -c /tmp/agenthub-webui.cookie \
  -H 'Content-Type: application/json' \
  -d '{"token":"<LAS_WEBUI_TOKENS 中的 token>"}' \
  http://127.0.0.1:18070/api/auth/login
```

响应中的 `csrf` 用于所有写请求的 `X-CSRF-Token`；cookie 文件含登录凭据，使用
完成后应删除。只读查询：

```bash
curl -b /tmp/agenthub-webui.cookie \
  'http://127.0.0.1:18070/api/interactions?status=pending'
```

逐次拒绝或回答也可直接调用 Web API：

```bash
curl -X POST -b /tmp/agenthub-webui.cookie \
  -H 'Content-Type: application/json' -H 'X-CSRF-Token: <csrf>' \
  -d '{"outcome":"rejected"}' \
  http://127.0.0.1:18070/api/interactions/INT-ID/respond

curl -X POST -b /tmp/agenthub-webui.cookie \
  -H 'Content-Type: application/json' -H 'X-CSRF-Token: <csrf>' \
  -d '{"answer":{"answers":[{"id":"question-id","selected":["选项"]}]}}' \
  http://127.0.0.1:18070/api/interactions/INT-ID/respond
```

WebUI 已启用签名 HttpOnly session cookie、SameSite=Strict、CSRF 和 RBAC。
`viewer` 只读，`operator` 可审批/回答/介入，`admin` 还可创建或撤销常驻授权。
compose 默认 `LAS_WEBUI_REQUIRE_AUTH=true`，缺 token 或 session secret 会拒绝启动；
即使误把 `LAS_WEBUI_HOST` 绑定到非 loopback，无认证配置也会拒绝启动。默认仍只
发布到宿主机 loopback。跨主机开放必须在可信反向代理上启用 HTTPS，同时设置
`LAS_WEBUI_COOKIE_SECURE=true`；不得以裸 HTTP 暴露到局域网或公网。

## 4. 日常操作速查

| 操作 | 命令 |
|---|---|
| 与 hermes 对话 | `./scripts/agentctl-host.sh chat`（宿主机直连）或 `docker compose run --rm agentctl chat`（容器） |
| 一次性指令 | `./scripts/agentctl-host.sh chat "<需求>"` |
| 任务/事件/agent 查询 | `./scripts/agentctl-host.sh status` / `task list` / `events` / `agent list` |
| 任务级审批（Web UI 之外） | `agentctl task approve <id>` / Web UI 审批中心 |
| 原生 Agent 交互 | WebUI「AGENT 交互」卡片；API 为 `GET /api/interactions?status=pending`、`POST /api/interactions/{id}/respond` |
| 常驻授权 | 对话中说"以后 X 类你自己批"；`agentctl grant list` 查看 |
| worker 日志 | `~/Library/Logs/agenthub-<agent>.log`（macOS launchd） |
| worker 重启 | `launchctl kickstart -k gui/$(id -u)/top.evergardenviolet.agenthub.<agent>` |
| 控制面日志 | `docker compose logs -f state-writer`（等） |
| 健康状态 | `docker compose ps`；详细探针：`docker inspect --format '{{json .State.Health}}' <container>` |

## 5. 升级与回滚

```bash
python3 scripts/control-plane-backup.py create \
  --output /path/to/local-backups --workspace "$HOME/AgentWorkspace"
git pull && docker compose build && docker compose up -d   # 升级
launchctl kickstart -k gui/$(id -u)/top.evergardenviolet.agenthub.codex  # worker 代码同仓，重启即生效
```

- 数据库迁移随 `state-writer` 启动自动执行（`migrations_pg/` 目录，幂等）；
  生产环境只有已验证备份生成的一次性回执存在且新鲜时才允许执行
- 回滚：`git checkout <旧 commit> && docker compose build && docker compose up -d`；
  迁移只增不改，旧代码读新库一般兼容
- 修改 `LAS_ADAPTER_TOKEN` 后：`kickstart` 重启 worker 即生效（agentctl 每次运行重读 .env）；切换窗口内进行中调用会 401

## 6. 故障排查

### 6.1 state-writer / janitor 崩溃循环
多为 PG 密码与既有数据卷不符：`docker compose logs state-writer` 见
`password authentication failed`。处理：把 `.env` 的 `LAS_PG_PASSWORD`
改回建卷时的值（或确认无数据后 `docker compose down -v` 清卷重建）。

### 6.2 hermes 连不上 LLM（ConnectError）
容器内 `127.0.0.1` 指容器自身。检查 compose 里 agentctl 的
`LAS_LLM_BASE_URL` 是否为 `http://host.docker.internal:8317/v1`（固定值，
不要从 .env 读——.env 是宿主机视角）。

### 6.3 codex 任务"成功"但没有产物（谎报）
历史上由 MCP 调用被自动取消导致（openai/codex#24135）。修复已内置：
MCP 只读工具带 `readOnlyHint`，`~/.codex/config.toml` 的 agent-* 服务需配
`default_tools_approval_mode = "approve"`。且 review_task 有产物核验 veto：
声明创建文件但无产物会被强制驳回返工，Web UI 任务详情可见驳回原因。

### 6.4 构建失败：拉不动镜像 / pip hash mismatch
Docker Hub 不可达：`--build-arg REGISTRY=docker.m.daocloud.io/library`（镜像源
必须保留相同 OCI digest，否则固定摘要校验会安全失败）；
PyPI 不稳：`--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`。

### 6.5 worker 显示 offline
agents 表在线判定按 `lease_expires_at` 动态计算。查 worker 进程与日志；
心跳间隔/租约由 `LAS_HEARTBEAT_INTERVAL` / `LAS_LEASE_TTL` 控制。

### 6.6 委派非 codex/kimi 的 agent 失败
gateway 路由表（`infra/agentgateway/config.docker.yaml`）目前只静态映射
codex/kimi/dsh。临时绕过：`LAS_GATEWAY_URL=` 置空走直连；长期方案是 gateway
动态路由（见 Evolution v3 §8.2）。

## 7. 备份

| 数据 | 位置 | 说明 |
|---|---|---|
| 状态库 | docker volume `pg-data` | 唯一事实源，定期 `pg_dump` |
| 事件流 | docker volume `nats-data` | JetStream，可从事件重建 |
| 任务产物 | docker volume `agent-data` + 宿主机 `$LAS_WORKSPACE` | 双份（adapter 侧为准） |
| 配置与密钥 | `.env`（600） | 单独妥善备份，勿入仓 |

创建一致性备份：

```bash
python3 scripts/control-plane-backup.py create \
  --output /path/to/local-backups \
  --workspace "$HOME/AgentWorkspace"
python3 scripts/control-plane-backup.py verify \
  /path/to/local-backups/agenthub-backup-*.tar.gz
```

脚本要求 PostgreSQL、NATS、State Writer 正在运行。它只暂停当前实际运行的写入
相关控制面服务，生成 PostgreSQL custom dump，再停 NATS 后复制一致的
JetStream 卷、`agent-data` 和宿主机 Workspace；随后先启动 NATS、再恢复原先
运行的服务。归档在校验所有文件大小、SHA-256 和 `PGDMP` 头后才原子发布，权限
固定为 600。`.env` 和其他密钥不会进入归档，必须通过独立的机密备份渠道保存。
成功备份还会向持久卷写入一次性 migration receipt。compose 固定启用
`LAS_REQUIRE_MIGRATION_BACKUP=true`：检测到新 migration 时，State Writer
先将回执原子改名为 `.consuming`；无回执、超过 24 小时、格式错误或崩溃遗留
的 consuming 状态都会拒绝迁移。迁移全部成功后回执删除，不能复用；正常捕获
的迁移错误会恢复回执，允许修复 migration 后基于同一安全备份重试。全新空库
没有旧数据可保护且尚不能运行备份脚本，因此首次 bootstrap 豁免此门禁；只要
已有任一 `schema_migrations` 版本，后续升级即强制执行。

`verify` 是离线操作，应定期在另一台机器和异地副本上执行。SHA-256 用于发现
损坏，不代表来源签名；在镜像签名批次完成前只能信任来自受控备份目录的归档。
自动化恢复流程如下；隔离环境中的实际恢复演练仍是发布阻断项。

受保护恢复（破坏性操作，必须在维护窗口执行）：

```bash
python3 scripts/control-plane-backup.py restore \
  /path/to/local-backups/agenthub-backup-20260820T000000000000Z.tar.gz \
  --workspace "$HOME/AgentWorkspace" \
  --safety-output /path/to/local-backups/pre-restore \
  --confirm RESTORE
```

恢复会先对当前状态自动创建并验证新的 safety backup；这一步失败时不会覆盖任何
数据。随后暂停控制面和 NATS，以 `pg_restore --clean --if-exists
--exit-on-error` 恢复数据库，并替换 JetStream/agent-data 卷。现有宿主机
Workspace 不删除，而是重命名为同级 `AgentWorkspace.pre-restore-<时间>` 后再
恢复目标版本。任何破坏性阶段错误都会让控制面保持停机，禁止半恢复状态继续
处理任务；应根据错误检查并用输出的 safety backup 回退。

隔离恢复演练使用随机 `COMPOSE_PROJECT_NAME`、独立临时卷且结束后 `down -v`，
不会连接默认 agentHub project：

```bash
LAS_RUN_RESTORE_DRILL=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_backup_restore_drill.py
```

2026-08-20 已完成一次真实隔离演练：PostgreSQL 行、NATS 卷文件、agent-data
卷文件和宿主机 Workspace 均先改写为 mutated，再成功恢复为 original；恢复前
safety backup 与旧 Workspace 可读取，最后确认随机项目的容器、网络和卷全部
删除。该测试现作为显式集成门禁保留。正式部署到另一台目标主机时仍须在其维护
窗口再执行一次，验证当地 Docker、磁盘权限和备份介质，而不是拿本次结果替代。

消息/路由故障门禁使用隔离端口并需显式开启：

```bash
LAS_RUN_GW=1 .venv/bin/python -m pytest tests/integration/test_agentgateway.py
.venv/bin/python -m pytest tests/integration/test_state_writer.py
LAS_RUN_PG_FAULTS=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_postgres_restart_fault.py
LAS_RUN_DSH_RESTART=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_dsh_restart_fault.py
```

前者包含 gateway 进程重启后的 A2A 幂等重放，后者包含 durable consumer 与 NATS
持久存储重启、重复 event_id 去重。PostgreSQL 用例创建随机 Compose project、
临时端口和卷，验证停库 NAK 与恢复后单次落库；DSH 用例使用随机端口和临时
`DSH_HOME`，只调用 session.create/list/history，验证 DSH 进程重启和 Adapter
实例重建，不调用模型且不改用户 `~/.dsh`。这些测试必须在允许监听 loopback
端口、启动隔离进程/容器的环境运行；不要改写测试使用临时资源的设计，也不要
把它们指向默认栈端口、用户 DSH_HOME 或生产数据目录。

2026-08-20 已在本机执行 agentgateway 隔离门禁并通过（6 passed）：临时进程
使用随机 gateway/worker 端口和仅驻留测试进程的随机 API key，三条 Agent 路由
均指向 fake worker，不访问默认 Adapter；覆盖 401、ACL 403、路由独立限流、
Hermes A2A 委派和 gateway 进程重启后的终态幂等重放。

2026-08-20 已在本机执行 NATS 隔离门禁并通过（2 passed）：每个用例使用独立
随机 loopback 端口与临时 JetStream 存储，覆盖端到端状态落库，以及 NATS 进程
携持久存储重启后 durable consumer 恢复、重复 `event_id` 只落一条 Event/Run。

2026-08-20 已在本机执行 PostgreSQL 隔离门禁并通过（1 passed）：停库期间
JetStream durable delivery 未被错误 ACK，数据库恢复后同一 `event_id` 仅落一条
Event/Run；测试结束时唯一 Compose project、容器、网络及临时卷均由清理阶段销毁。

2026-08-20 已在本机执行 DSH 隔离门禁并通过（1 passed）：真实 DSH Web 进程
在随机端口重启后，临时原生 session 仍可被新 Adapter 实例 list/history/resume。
这项结果只覆盖 DSH 真实进程与 Adapter 实例重建，不替代 Adapter 服务进程重启
及真实模型双轮恢复门禁。

## 8. 安全基线

- 所有端口绑 `127.0.0.1`（Web UI / gateway / Jaeger 默认不暴露局域网）
- worker 需要容器回连时才加 LAN IP 到 `LAS_ADAPTER_BIND`，且必须配 `LAS_ADAPTER_TOKEN`
- `never_grant` 高危操作（删库、 force push 等）只能逐次人工批准，见 `src/hermes/policy.py`
- gateway `apiKey.agents` ACL 控制 hermes 可访问的 agent 列表
- gateway 每条 Agent 路由有独立本地 token bucket（30 次突发、每分钟补充
  30 次）；超过额度返回 429，避免失控协作循环持续放大调用
- 跨主机 gateway 使用独立 TLS 1.3/mTLS + strict JWT 剖面；生产预检拒绝
  非 loopback HTTP、缺失身份材料或权限过宽的 JWT/client key
- WebUI 使用高熵 token、签名 HttpOnly Cookie、CSRF 与 `viewer/operator/admin`
  RBAC；跨主机只允许经带登录限流的 HTTPS 反向代理访问
- Orchestrator A2A 入口在 compose 中强制认证；认证全空、生产 token 少于
  16 字符或非 loopback 无认证监听都会启动失败
- 关键控制面服务具备 dependency-aware readiness，容器设 CPU/内存/PID 上限，
  `json-file` 日志自动轮换（10MB × 5）
- `scripts/production-preflight.py` 在启动前检查 `.env` 权限、默认/弱密钥、
  WebUI RBAC 配置和 A2A peer 映射，且输出永不包含 secret value
