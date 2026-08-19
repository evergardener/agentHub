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
| `LAS_ADAPTER_BIND` | worker 监听地址，默认 `127.0.0.1`；需容器回连时加宿主机 LAN IP |
| `LAS_DSH_PERMISSION_PRESET` | 当前必须为 `read-only`；ActionIntent 回执已接通，但 tool target normalization/脱敏完成前拒绝更宽 preset |
| `LAS_DATABASE_URL` | 留空 = compose PG；`sqlite:////path/x.db` = SQLite；外部 PG 直接填 URL |
| `LAS_OTEL_ENDPOINT` | compose 内已指向 jaeger；置空关闭 tracing |

### 3.3 启动控制面

```bash
docker compose up -d
docker compose ps          # 全部 Up / healthy
```

入口：
- Web UI（看板/审批/事件流/复审记录）：http://127.0.0.1:18070
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

### 3.6 DSH/Kimi 原生交互闭环

DSH 产生 approval/question 后，任务进入 `blocked`，WebUI「AGENT 交互」卡片
实时显示请求。审批只能选择本次允许或拒绝；本次允许会先由控制面决策关联的
ActionIntent，再把不可伪造的 receipt 送到 DSH `/api/respond`。问题以 DSH
定义的 question id 和选项结构整批回答，二者均继续原 native session/turn。
此时旧的任务级 approve/reject 会明确拒绝，避免只修改数据库状态却没有回应
原生 Runtime。

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

只读查询：

```bash
curl 'http://127.0.0.1:18070/api/interactions?status=pending'
```

逐次拒绝或回答也可直接调用 Web API：

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"outcome":"rejected"}' \
  http://127.0.0.1:18070/api/interactions/INT-ID/respond

curl -X POST -H 'Content-Type: application/json' \
  -d '{"answer":{"answers":[{"id":"question-id","selected":["选项"]}]}}' \
  http://127.0.0.1:18070/api/interactions/INT-ID/respond
```

当前 WebUI 仍是 loopback-only、无账号体系；不得反向代理到局域网或公网。生产
开放前必须完成 Phase 5 的认证、CSRF 与 RBAC。

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

## 5. 升级与回滚

```bash
git pull && docker compose build && docker compose up -d   # 升级
launchctl kickstart -k gui/$(id -u)/top.evergardenviolet.agenthub.codex  # worker 代码同仓，重启即生效
```

- 数据库迁移随 `state-writer` 启动自动执行（`migrations_pg/` 目录，幂等）
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
Docker Hub 不可达：`--build-arg REGISTRY=docker.m.daocloud.io/library`；
PyPI 不稳：`--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`。

### 6.5 worker 显示 offline
agents 表在线判定按 `lease_expires_at` 动态计算。查 worker 进程与日志；
心跳间隔/租约由 `LAS_HEARTBEAT_INTERVAL` / `LAS_LEASE_TTL` 控制。

### 6.6 委派非 codex/kimi 的 agent 失败
gateway 路由表（`infra/agentgateway/config.docker.yaml`）目前只静态映射
codex/kimi。临时绕过：`LAS_GATEWAY_URL=` 置空走直连；长期方案是 gateway
动态路由（见 Evolution v3 §8.2）。

## 7. 备份

| 数据 | 位置 | 说明 |
|---|---|---|
| 状态库 | docker volume `pg-data` | 唯一事实源，定期 `pg_dump` |
| 事件流 | docker volume `nats-data` | JetStream，可从事件重建 |
| 任务产物 | docker volume `agent-data` + 宿主机 `$LAS_WORKSPACE` | 双份（adapter 侧为准） |
| 配置与密钥 | `.env`（600） | 单独妥善备份，勿入仓 |

## 8. 安全基线

- 所有端口绑 `127.0.0.1`（Web UI / gateway / Jaeger 默认不暴露局域网）
- worker 需要容器回连时才加 LAN IP 到 `LAS_ADAPTER_BIND`，且必须配 `LAS_ADAPTER_TOKEN`
- `never_grant` 高危操作（删库、 force push 等）只能逐次人工批准，见 `src/hermes/policy.py`
- gateway `apiKey.agents` ACL 控制 hermes 可访问的 agent 列表
