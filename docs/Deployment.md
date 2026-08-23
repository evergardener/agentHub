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
│  codex :8201   dsh :8203（launchd 常驻，token 鉴权）；kimi 当前禁用        │
│  Codex CLI、DSH Web :3080、LLM 端点（127.0.0.1:8317）用户自装              │
└─────────────────────────────────────────────────────────────────────────────┘
```

核心原则：
- worker agent **不进容器**——用宿主机原生环境与授权，经心跳自注册，没注册就不可用
- `agents.yaml` 的 `enabled` 是初始目标状态；管理员在 WebUI `AGENTS` 卡片设置的
  数据库开关（`agent_controls`）优先级更高，二者都优先于 worker 心跳；
  停用 Agent 的心跳只保留审计事件，不注册、不续租、不发现能力，也不能委派任务。
  用户点名停用 Agent 时，Hermes 必须先询问是启用后重新探测，还是改派其他 Agent；
  确认前不得创建计划任务或静默改派。重新启用会先清除旧租约并显示「等待注册」，
  只有收到新心跳后才恢复在线和可委派状态。
- 密钥只走环境变量 / `.env`（权限 600），不入库、不入仓、不用 Keychain
- 状态唯一事实源是 PostgreSQL（可选 SQLite）；NATS 只是事件总线

## 2. 前置条件

| 依赖 | 要求 |
|---|---|
| Docker + compose 插件 | Docker Desktop（macOS）或 Docker Engine 24+（Linux） |
| LLM 端点 | OpenAI 兼容接口（本项目用本地 cliproxy `127.0.0.1:8317`） |
| worker runtime | 按需自装：Codex CLI 与 DSH；DSH Adapter 要求先运行仅回环监听的 DSH Web |
| 网络 | 容器可回连宿主机（compose 已配 `host.docker.internal:host-gateway`） |

## 3. 部署步骤

### 3.1 获取代码 / 镜像

```bash
git clone git@github.com:evergardener/agentHub.git
cd agentHub   # 下文统称项目根
```

仓库名称与推荐目录名是 `agentHub`，`docker-compose.yml` 的 Compose 项目身份为
`agenthub`。现有 `local-agent-system` 部署升级时，这会创建新的 `agenthub_*`
命名卷，不能直接空栈启动后视为迁移完成。先按 7.2 节创建并验证一致性备份，再
恢复到新命名卷；在业务验收完成前保留旧卷、迁移前镜像标签和旧 Compose 文件，
以便停止新栈后恢复历史项目身份。

镜像两种来源：
- **本地构建**（默认）：`docker compose build`；Docker Hub / PyPI 不可达时：
  ```bash
  docker compose build \
    --build-arg REGISTRY=docker.m.daocloud.io/library \
    --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
  ```

容器依赖由 `requirements.lock` 精确锁定，且依赖层位于源码层之前；普通源码变更
不会重新解析或下载所有 wheel。升级 `pyproject.toml` 依赖时必须同步刷新锁文件，
在两个目标架构构建并完成全量测试、镜像扫描和本节生产烟测后才能提交。
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
# 已有 .env 时建议使用初始化器：会先备份旧文件、生成本机密钥、移除 Kimi
# peer、加入 DSH peer；不会轮换 PostgreSQL 密码，也不会打印任何密钥值。
.venv/bin/python scripts/init-production-env.py
```

初始化器把首次登录信息写入 owner-only 的
`runtime/production-credentials.json`（被 Git 忽略）。请在部署完成后把其中凭据
转存到你的密码管理器，再删除该 bootstrap 文件。

已有 PostgreSQL 数据卷时，不要只修改 `.env`。先创建并验证控制面备份，再用
备份门控的轮换命令同步更新数据库角色和 `.env`；命令不会输出新密码：

```bash
python3 scripts/rotate-postgres-password.py \
  --backup backups/pre-production/agenthub-backup-<timestamp>.tar.gz
```

新密码同时写入仅 owner 可读的 `runtime/production-credentials.json`，部署完成后
请将它转移到正式的 Secret Manager。

必填项（其余见 .env.example 注释）：

| 变量 | 说明 |
|---|---|
| `LAS_LLM_BASE_URL` | **宿主机视角**的 LLM 地址（如 `http://127.0.0.1:8317/v1`）；容器侧由 compose 固定改写为 `host.docker.internal`，不要在本文件填容器地址 |
| `LAS_LLM_API_KEY` | LLM 端点密钥 |
| `LAS_LLM_MODEL` | 模型名（如 `deepseek-ai/DeepSeek-V4-Flash`） |
| `LAS_PRODUCTION_MODE` | 生产必须为 `true`；会在运行时拒绝 DSH 开发豁免，防止跳过预检绕过安全边界 |
| `LAS_GATEWAY_API_KEY` | gateway 认证 key，`openssl rand -hex 24` 生成；**留空 gateway 拒绝所有请求** |
| `LAS_PG_PASSWORD` | PostgreSQL 密码；**已有数据卷时改它会导致认证失败**（见 §6.2） |
| `LAS_ADAPTER_TOKEN` | 留空即可——worker 首启自动生成随机值回写本文件 |
| `LAS_ACTION_RECEIPT_SECRET` | ActionIntent receipt HMAC 密钥；生产用 `openssl rand -hex 32` 独立生成；暂时可回退 adapter token |
| `LAS_API_TOKEN` / `LAS_A2A_PEERS` | 外部 Hermes A2A 身份；`LAS_A2A_PEERS` 是 token→peer，不绑定 worker |
| `LAS_HERMES_GATEWAY_API_KEY` | qishuo 访问 gateway `/agenthub` 的外部 token |
| `LAS_HERMES_BACKEND_TOKEN` | gateway 注入 Orchestrator 的内部 token；是 `LAS_A2A_PEERS` 的 key，不交给 qishuo |
| `LAS_WEBUI_TOKENS` | WebUI 登录 token→role JSON；token 用 `openssl rand -hex 24` 生成，role 为 `viewer` / `operator` / `admin` |
| `LAS_WEBUI_SESSION_SECRET` | WebUI 签名 session cookie 的独立 HMAC 密钥，使用 `openssl rand -hex 32`；未配置时 WebUI 拒绝启动 |
| `LAS_ADAPTER_BIND` | worker 监听地址，默认 `127.0.0.1`；需容器回连时加宿主机 LAN IP |
| `LAS_DSH_PRODUCTION_ENABLED` | 当前 Codex + DSH 发布候选必须为 `true`；Adapter 启动每个 session 时重新验证原生权限链 |
| `LAS_DSH_ALLOW_UNVERIFIED_RUNTIME` | 旧版开发兼容项；生产必须为 `false`，新 Adapter 不依赖它放行 prompt |
| `LAS_DSH_PERMISSION_PRESET` | 必须为 `read-only`；Adapter 用 `commands.execute` 应用并核验原生 permission/sandbox/approval 状态 |
| `LAS_DSH_AGENT_PRESET` | 必须为 `standard`；`minimal` 的 `str_replace_editor` 已实测绕过 read-only |
| `LAS_DATABASE_URL` | 留空 = compose PG；`sqlite:////path/x.db` = SQLite；外部 PG 直接填 URL |
| `LAS_OTEL_ENDPOINT` | compose 内已指向 jaeger；置空关闭 tracing |

填写完成后运行生产预检。它只输出变量名和修复建议，不输出任何密钥值：

```bash
python3 scripts/production-preflight.py .env
# HTTPS 反代部署使用严格模式（loopback HTTP 的 cookie warning 也视为失败）
python3 scripts/production-preflight.py --strict .env
```

预检同时读取镜像/仓库的 `config/agents.yaml`，要求当前发布候选保持
Codex/DSH enabled、Kimi disabled；使用外置 catalog 时通过 `--agents-file`
指定实际部署文件，不得只检查仓库样例。

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
- **外部总控 A2A 端点**（qishuo 生产接入）：http://127.0.0.1:8300/agenthub，
  契约见 [docs/orchestrator-a2a.md](orchestrator-a2a.md)。支持 A2A v1.0
  `SendMessage`（Bearer caller token，Registry 动态发现 worker）与
  legacy `message/send`（`X-Agent-Token`，`LAS_API_TOKEN`
  回退 `LAS_ADAPTER_TOKEN`）；审批走 `tasks/approve` / `tasks/reject`
- 与 hermes 对话（二选一）：
  - 容器模式：`docker compose run --rm agentctl chat`
  - **宿主机直连**：`./scripts/agentctl-host.sh chat`——hermes 就是本仓库的
    Python 模块，不必须在容器里跑；基础设施已映射到 127.0.0.1，包装脚本
    自动把 .env 翻译为宿主机视角（PG/gateway 地址改写）。前提是宿主机
    已 `pip install -e .`（注册 `agentctl` 入口点）。

WebUI 的 `AGENTS` 卡片以 `config/agents.yaml` 生产目录为权威来源：仅显示目录内
Agent，并以相同卡片样式展示已启用和已停用状态。管理员可手动切换目标状态；停用
后不再探测注册，启用后等待新心跳。Registry 中遗留的集成测试 worker（例如
`fake`）不会混入生产列表。任务详情的「委派指令（原文）」直接显示 `tasks.objective`，不做
前端总结；结构化 Task Plan 会同时显示步骤、预期操作和验收条件。未绑定持久
collaboration 的旧任务只能展示当时保存的单条目标，WebUI 会明确标注无法还原更长
的上游会话。

WebUI 使用浅色磨砂三栏工作区：左侧是 Agent 状态和可筛选、可滚动的 Session
导航，中间是类似 Codex 的持久长对话视图，右侧固定显示所选会话的关联任务详情；
审批、原生 Agent 交互、常驻授权和事件流集中在顶栏「操作中心」抽屉。中央会话按
Collaboration 展示完整的用户、Hermes 与工具消息序列、`context_revision`、关联任务
和可恢复 Agent Session，适合核验跨天续接是否仍在同一上下文。点击左侧带
Collaboration 的 Session 会同步切换中央会话，并在右侧打开首个关联任务；没有创建 Task 的纯聊天仍
可在会话选择器查看，但不会伪造成可审批、可执行的任务。Artifact 按钮会先标识文件
可用性，点击后在右侧任务详情内显示加载状态、选中状态和内容；缺失或越界文件不会伪装
成可点击产物。

### 3.3.1 告警与通知

Janitor 的租约过期、执行超时、产物缺失，以及重试耗尽的任务会写入持久
`alerts` outbox，并产生 `system.alert` 审计事件。WebUI 顶栏显示未处理数量，点击
后在独立告警抽屉中按严重程度筛选；每条记录给出问题解释、影响、建议动作和折叠的
技术详情，不会把 Agents、审批和任务卡片向下挤出首屏。`operator`/`admin` 可
「标记已知」，`viewer` 只读；标记已知只从待处理列表移除，**不会**修复、重试或
取消任务，应先按建议完成处置。同一 kind/task/detail 只建立一条告警，重复发生
增加 `occurrences`，不会形成通知风暴。
宿主 Adapter 的 `${HOME}/AgentWorkspace` 以同一绝对路径只读挂载到 janitor，
避免容器因看不到真实文件而误报 `artifact_missing`；文件恢复可见后，对应的 open
条件告警会由 janitor 自动转为 `resolved`，无需用户把误报逐条标记已知。
Janitor 只对 `LAS_ARTIFACT_ROOTS` 逗号分隔的明确受管根目录执行存在性检查；
Compose 默认为 `/data/workspace,${HOME}/AgentWorkspace`。历史测试留下的
`/tmp` 或 macOS 临时目录路径仍保留在数据库用于审计，但不再触发生产缺失告警；
旧的 open 误报会自动转为 `resolved`。
若历史告警引用的任务记录已经不存在，WebUI 会保留告警及技术详情用于审计，但不再
显示「打开任务」按钮，避免跳转到必然返回 `not found` 的页面。

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

# DSH Web 固定只监听回环；随后安装 DSH Adapter；Kimi 当前不要安装
./scripts/install-dsh-web-autostart.sh
./scripts/install-agent-autostart.sh dsh
```

验证：

```bash
docker compose run --rm agentctl agent list    # 目标：codex/dsh online，kimi disabled
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

2026-08-20 真实门禁先证实把 `/permission read-only` 作为 prompt 是错误实现，
随后从 DSH Web 客户端定位到 `commands.execute` 专用 RPC。Adapter 现在对新建和
恢复 session 先执行该命令，再从 history 核验 `permission=read-only`、
`sandbox=read-only`、`approval=ask`；失败时不会发送模型 prompt。真实工具门禁
同时发现默认 `minimal` preset 的 `str_replace_editor` 可绕过 read-only，而
`standard` preset 的 write 会先因沙箱拒绝，再携带精确 diff 发出 approval。
因此 AgentHub 强制 `standard`，并通过 `/api/events.mux` WebSocket 获取稳定
`rpcId`。真实拒绝已验证文件不落盘；真实 signed ActionIntent `allowed-once` 已
验证只在批准后创建临时文件。生产 catalog/peer 尚待下一批统一切换。

创建需要检查或修改真实项目的任务时，Hermes 必须在 agentHub
`tasks/create` 命令中显式传入绝对、非根目录的 `workspace`。控制面将其持久化到
Task 上并按 Agent Profile 的 `workspace_roots` 校验；DSH Adapter 随后调用原生
`workspace.create({path})` 注册或复用工作区，再以
`session.create({workspaceId})` 创建会话，因此该会话会归入 DSH 对应工作区，而
不是「未分组」。未传 `workspace` 的任务继续使用隔离的 AgentHub task workspace，
不会自动猜测或沿用历史目录。

`workspace` 只定义执行和目标路径边界，不授予写权限。DSH 仍以
`read-only / ask` 启动；每次写入仍须生成可检查的 ActionIntent。只有操作位于
Profile allowlist、目标在当前 task workspace 内且提供回滚方案时，Hermes 才能
签发本次 `allowed-once`；删除、Git 提交/推送、部署、数据库写入及未知操作继续
fail-closed 并要求用户决定。生产升级时必须先备份 Profile，再对现有
`AP-DSH-REVIEW` 做版本化更新并配置明确的 `workspace_roots`。修改
`config/agent_templates.yaml` 只影响新 seed，不会覆盖生产数据库里的已有 Profile。

Codex 使用同一个 Task `workspace` 作为 App Server thread 的 `cwd` 和唯一
`runtimeWorkspaceRoots`。Adapter 会在 `thread/start`、`thread/resume` 及进程重连
时核验 App Server 返回的 cwd；不一致即停止。控制文件仍写入隔离的 AgentHub task
目录，不会向真实项目写入 `context.md`。交付物只收集本轮原生 `fileChange` 事件明确
列出的工作区内文件，不递归复制整个真实项目。同一 native thread 不允许切换
workspace；需要更换目录时创建替代 Session。

Codex 原生审批由 State Writer 重新计算结构化语义，不信任 Adapter 提供的
`inspectable` 标记。工作区内、包含可逆原生 patch 的新增/更新可按 Profile 路由给
Hermes 单次批准；删除、目录移动、额外权限、未知命令、工作区外路径或缺少可验证
回滚证据的操作继续要求用户确认。生产 Codex Profile 同样必须配置明确的
`workspace_roots`。

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
通过；当前 `config/agents.yaml` 与 production-preflight 均排除 Kimi，额度恢复或
升级前不要自动重试，恢复后重新运行
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
LAS_RUN_CODEX_REJECT=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_codex_rejection_fault.py
LAS_RUN_CODEX_SERVICE_RESTART=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_codex_service_restart_fault.py
```

2026-08-20 已在 pytest 临时工作区真实执行通过（1 passed）：Codex 的修改请求
保持挂起，测试以绑定当前原生 request/thread/context revision 的签名 receipt
逐次 `allowed-once`，随后验证真实源码产物与 pytest。该用例不覆盖拒绝、双轮
恢复或 Adapter 服务进程重启。2026-08-20 第二条门禁也已真实通过（1 passed）：
关闭第一 Adapter/App Server 后，新实例以相同 native thread 完成第二轮并准确
复述第一轮 marker；它仍不替代 HTTP Adapter 服务进程重启测试。同日第三条
拒绝门禁真实通过（1 passed）：所有挂起的原生修改请求均明确拒绝，目标文件及
`workspace/*` 产物均不存在。同日第四条随机端口 HTTP Adapter 整进程重启门禁
真实通过（1 passed）：停止第一服务后，第二服务以持久 native thread ID 和提升
后的 context revision 恢复第二轮，不访问默认 8201。

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

### 5.1 历史 A2A 孤儿任务修复

旧版 Orchestrator 通过统一 `agenthub` peer 创建的 Task 可能没有
`collaboration_id`。新版本只修复后续 `tasks/create`；历史数据必须在独立维护窗口
按证据逐条处理，禁止将所有 `collaboration_id IS NULL` 的任务批量挂到一个 Session。

先停止会产生新任务的入口并创建、验证控制面备份。由操作者结合 gateway/Hermes
记录制作 manifest；每项必须同时匹配 task ID、已认证 peer、A2A context、目标
Agent、数据库中的精确创建时间和完整 objective 的 SHA-256：

```json
{
  "version": 1,
  "entries": [
    {
      "task_id": "T-YYYYMMDD-NNNN",
      "peer": "qishuo",
      "context_id": "ctx-from-hermes-a2a",
      "agent": "dsh",
      "objective_sha256": "64-lowercase-hex-characters",
      "created_at": "exact-value-from-tasks.created_at",
      "evidence": {"gateway_request_id": "operator-reviewed-reference"}
    }
  ]
}
```

默认命令仅连接现有 schema 并输出计划，不运行 migration，也不写业务表。只有所有
条目均为历史终态且证据完全匹配时，顶层 `eligible` 才为 `true`：

```bash
.venv/bin/python scripts/backfill-a2a-collaborations.py \
  --manifest /secure/path/a2a-backfill.json
```

确认备份、计划和数据库目标无误后，才可显式 apply；receipt 路径必须不存在：

```bash
.venv/bin/python scripts/backfill-a2a-collaborations.py \
  --manifest /secure/path/a2a-backfill.json \
  --apply \
  --confirmation BACKFILL_A2A_COLLABORATIONS \
  --receipt /secure/path/a2a-backfill-receipt.json
```

整份 manifest 在单一数据库事务中执行。它只补 Conversation/Collaboration、Task
关联、历史 request/result 消息、可从既有事件恢复的 Session binding 和审计事件；
不改写原 Task 状态、结果、runs、events 或 artifacts。重复 apply 返回首次审计中
保存的原 receipt，不重复创建数据。receipt 必须和备份一起保留。

若验证失败，先停止新流量，再使用首次 receipt 回滚：

```bash
.venv/bin/python scripts/backfill-a2a-collaborations.py \
  --rollback \
  --receipt /secure/path/a2a-backfill-receipt.json \
  --confirmation ROLLBACK_A2A_COLLABORATIONS
```

回滚仅删除 receipt 明确列出的补写行并把这些 Task 恢复为 NULL 关联，同时保留补偿
审计。如果 Collaboration 已出现新 Task、消息、Session binding、interaction、
action intent 或 plan，命令会拒绝回滚；此时应从维护窗口备份恢复或制定人工迁移，
不得强删会话数据。若 apply 后 receipt 文件写出前进程异常，可从对应
`task.collaboration.backfilled` 事件的 `payload.receipt` 恢复首次 receipt。

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
当前发布候选的目标状态是 Codex/DSH `online`、Kimi `disabled`。DSH 若显示
offline，检查 WebSocket 426 修复后的 Adapter 版本、standard preset、read-only
权限核验和本地 DSH Web；不要用旧开发豁免绕过失败。

`disabled` 不是 `offline`：前者是人工策略门禁，即使 Adapter 仍发送有效心跳也
不会恢复在线或参与路由；后者表示已启用但当前租约过期。

### 6.6 新 Agent 不能委派

不要给 qishuo 或 gateway 添加 Agent 专用路由。检查 Adapter 心跳、Registry
租约、人工 enabled 开关和 Agent Profile。gateway `/agents/{id}` 是通配路由，
目标 endpoint 由 Registry 解析。

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
LAS_RUN_DSH_SERVICE_RESTART=1 \
  .venv/bin/python -m pytest -q \
  tests/integration/test_dsh_service_restart_llm.py
# 先在隔离/维护端口启动使用现有已授权配置的 dsh web，再运行：
LAS_RUN_DSH_APPROVAL=1 LAS_DSH_WEB_URL=http://127.0.0.1:<port> \
  .venv/bin/python -m pytest -q \
  tests/integration/test_dsh_native_approval.py
```

前者包含 gateway 进程重启后的 A2A 幂等重放，后者包含 durable consumer 与 NATS
持久存储重启、重复 event_id 去重。PostgreSQL 用例创建随机 Compose project、
临时端口和卷，验证停库 NAK 与恢复后单次落库；DSH 用例使用随机端口和临时
`DSH_HOME`，只调用 session.create/list/history，验证 DSH 进程重启和 Adapter
实例重建，不调用模型且不改用户 `~/.dsh`。服务重启项使用现有 DSH 配置调用
模型，同时重启随机端口 DSH Web 与 HTTP Adapter，并会新增可追溯测试 session。
原生审批项使用临时 AgentHub workspace，依次验证拒绝不落盘与 signed
`allowed-once` 批准后落盘；也会在用户 DSH storage 中新增可追溯测试 session。所有
这些测试必须在允许监听 loopback
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
