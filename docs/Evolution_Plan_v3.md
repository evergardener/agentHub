# 演进方案 v3：配置化、容器化与 Hermes 总控交互层

> 基于 v2 设计文档全部阶段落地后的现状（2026-08-17，commit 83839a4），
> 针对六条新要求逐条评估并给出实施方案。
> 与 v2 的关系：v2 的架构边界（A2A 通信 / NATS 事件 / SQLite或PG 状态 /
> MCP 工具 / Hindsight 记忆）不变，本方案只改**打包形态、配置来源、交互入口**。

---

## 0. 逐条评估结论

| # | 要求 | 评估 | 工作量 |
|---|------|------|--------|
| 1 | 模型走 OpenAI 兼容端点，env 可配 baseURL/key/model | 现状已接近（kimi runner 已 env 化），需统一命名并覆盖 codex 侧 | 小 |
| 2 | 密钥改为环境变量，弃用 Keychain | 可行；注意 .env 文件权限与泄漏面（见 §2 风险） | 小 |
| 3 | Docker 封装 + GitHub Actions 构建推 ghcr | 可行；最大变量是 codex CLI 的镜像内分发（见 §3 风险） | 中 |
| 4 | 默认 PostgreSQL（compose），env 可切外部 PG 或 SQLite | 可行；SQLite 方言耦合面很小（实测仅 ~8 处），可低成本双后端 | 中 |
| 5 | OTel tracing + Web UI | 功能定义见 §5；trace_id 骨架已存在，接入成本低 | 中 |
| 6 | Hermes 总控交互层 | 方向与 v2 完全一致；**用户担心的超时问题在当前 PoC 真实存在**，必须先做 A2A 异步化（§6.3） | 中-大 |

---

## 1. 模型配置统一（OpenAI 兼容 + env）

统一三组环境变量（所有 LLM 消费方共用），per-adapter 可覆盖：

```bash
LAS_LLM_BASE_URL=http://127.0.0.1:8317/v1     # OpenAI 兼容端点
LAS_LLM_API_KEY=sk-...                         # 端点密钥
LAS_LLM_MODEL=deepseek-ai/DeepSeek-V4-Flash    # 默认模型
# 可选覆盖：
KIMI_MODEL=...        # kimi adapter 专用模型
CODEX_MODEL=...       # codex adapter 专用模型
```

- `kimi` runner：改读 `LAS_LLM_*`（`KIMI_API_BASE/KIMI_MODEL` 保留为兼容别名）。
- `codex` adapter：容器入口脚本用 env 生成 `~/.codex/config.toml`
  （`model_providers.custom` + `env_key`），不在镜像里写死任何端点。
- Hindsight 的 `HINDSIGHT_API_LLM_*` 同源注入。

## 2. 密钥管理：环境变量

- 所有 Keychain 读取点（`cliproxy-api-key` / `gateway-api-key` /
  `hindsight-api-key` 共 4 处）改为**只读环境变量**。
- 本地开发：项目根 `.env`（gitignore，权限 600）+ `.env.example` 入库；
  compose 用 `env_file: .env`。
- 风险声明（写入 README）：env 方案下同用户权限的进程可读环境变量，
  泄漏面大于 Keychain；缓解措施是 `.env` 600 权限、容器内不落盘、
  CI 用 GitHub Secrets 注入。

## 3. Docker 化与镜像流水线

### 3.1 镜像划分（2 个镜像）

| 镜像 | 内容 |
|---|---|
| `ghcr.io/evergardener/agenthub-runtime` | Python 应用本体：orchestrator / adapters / state-writer / janitor / agentctl / hermes-brain |
| `ghcr.io/evergardener/agenthub-codex` | runtime + codex CLI（见风险） |

NATS / PostgreSQL / agentgateway / Hindsight 用官方镜像，compose 编排，不自建。

### 3.2 compose 服务（`deploy/docker-compose.yaml`）

```text
postgres        官方 pg:17，数据卷
nats            官方 nats:2 + JetStream 卷
agentgateway    cr.agentgateway.dev/agentgateway + 挂 config.yaml
hindsight       复用现有镜像（可选 external）
state-writer    runtime 镜像
janitor         runtime 镜像
codex-adapter   codex 镜像，127.0.0.1:8201
kimi-adapter    runtime 镜像，127.0.0.1:8202
hermes          runtime 镜像（总控入口，见 §6）
```

容器内访问宿主机 cliproxy：`host.docker.internal:8317`
（compose 加 `extra_hosts: host.docker.internal:host-gateway`）。

### 3.3 GitHub 流水线

- 远端：`git@github.com:evergardener/agentHub.git`（SSH 免密已配）。
- 工作流 `.github/workflows/image.yaml`：push main / tag 触发 →
  buildx 构建两个镜像 → 推 `ghcr.io/evergardener/agenthub-*`
  （`:latest` + `:sha-<short>` + tag 版本）；认证用 `GITHUB_TOKEN`
  （packages: write），无需额外密钥。
- CI 前置：`pytest` 离线套件必须绿才构建。

### 3.4 风险

- **codex CLI 分发**：本机 codex 来自 ChatGPT.app 内置；镜像内需改用
  npm 公开包安装（构建时验证可用性），若受许可/分发限制则降级为
  「codex adapter 跑在宿主机、其余容器化」的混合拓扑，compose 用
  `extra_hosts` 指回宿主机。此风险在 M2 第一天验证。
- agentgateway 镜像平台差异（arm64/amd64）由 buildx `--platform` 固定。

## 4. 数据库后端可切换

- 统一入口 `LAS_DATABASE_URL`：
  `postgresql://user:pass@postgres:5432/agenthub`（compose 默认）
  | `postgresql://...外部...`（自建 PG）
  | `sqlite:////data/agent-state.db`（轻量/单机回退）。
- 现状耦合面实测很小：`state/db.py` + `state_store.py` + `janitor.py`
  类型标注 + 8 处方言（`datetime('now')`、`rowid`、upsert）。
- 实施：`state/db.py` 抽象 `connect(url)` 返回适配连接
  （PG 用 `psycopg`）；迁移文件拆方言或改写成两边兼容的 DDL；
  `agentctl events` 的 `rowid` 游标改为自增 `seq` 列（两个后端通用）。
- 测试矩阵：离线套件默认 SQLite；`LAS_TEST_PG=1` 时对 compose 起的
  PG 再跑一遍；CI 用 services: postgres 跑双后端。

## 5. 可观测性

### 5.1 OTel tracing（要做什么）

- 目标：一条用户请求从 Hermes 到 Worker 到 LLM 的**完整耗时与失败归因**。
- Span 设计：`task.create → task.delegate → gateway 转发 → adapter 执行
  → llm.call / tool.call → task.review`；`trace_id` 沿用现有
  `trace-<root_id>`（事件里已贯通），直接映射为 OTel trace。
- 落地：`opentelemetry-sdk` + OTLP exporter；compose 加 Jaeger
  all-in-one（Web 查询界面自带）；gateway 层 tracing 由其 OTel 输出对接。
- 验收：`agentctl submit` 一次完整链路，在 Jaeger 里能看到单 trace
  全跨度树。

### 5.2 Web UI（要做什么）

只读为主 + 审批操作，替代日常敲 CLI：

- **Dashboard**：Agent 在线状态/租约倒计时、任务按状态分列看板。
- **任务详情**：状态时间线、runs 历史、artifacts 查看/下载、关联事件。
- **事件流**：`events` 表 SSE 实时推送（等价 `agentctl events --follow`）。
- **审批中心**：blocked（input-required）任务列表 + 批准/拒绝按钮
  （对接已有 approve/reject）。
- 技术：FastAPI 复用 state_store 出 JSON/SSE；前端轻量单页
  （htmx 或 React，不做账号体系，loopback only）。

## 6. Hermes 总控交互层

### 6.1 形态

新增常驻服务 **hermes-brain**（runtime 镜像内）：

```text
你 ──► hermes（CLI chat / Web UI chat，后续可接 IM bot）
        │  LLM（LAS_LLM_*）+ 工具调用循环
        ├─ create_task / delegate / wait / review / retry / cancel
        ├─ registry（选 Worker）/ memory（Hindsight，唯一写方）
        ├─ MCP 工具（filesystem/git/browser，自查环境）
        └─ 审批策略引擎（§6.2）
```

你的例子（部署 ghcr 镜像）在该形态下的实际路径：
hermes 分析需求 → 派 research 子任务给 kimi 搜集镜像/环境信息 →
hermes 汇总方案 → **判定为部署类（写操作）→ 请求你审批** → 批准后派给
kimi/codex 执行 → 完成事件回传 → hermes 复审（可再派核查子任务）→
有问题返工（attempt+1）→ 最终汇报给你。

### 6.2 审批策略（简单任务不打扰你）

`config/permissions.yaml` 声明风险分级，hermes 决策时查询：

```yaml
auto_approve:        # 只读/查询类：hermes 直接批准
  - 信息检索 / 文件读取 / 状态查询 / 分析总结
require_user:        # 写操作：进 blocked，等你批准
  - 文件修改 / 代码变更 / 部署 / 删除 / 外部发布
```

任务进 `blocked` 后你在 Web UI 审批中心或 CLI 一键放行
（§Phase 8 已实现 approve/reject 机制，正好接上）。

### 6.2.1 对话内审批与常驻授权（2026-08-17 补充）

审批不只发生在 Web UI——**hermes 在对话里直接问你**，三种应答：

```text
hermes: 任务 T-xxx 需要重启服务 nginx，是否批准？
你:     批准            → 本次放行（blocked → working）
你:     拒绝            → 取消（blocked → cancelled，级联）
你:     以后重启类你自己批 → 常驻授权（standing grant）
```

**常驻授权（standing grant）**：

- 授权对象按「操作类型」而非单个任务（如：重启服务、容器起停、
  日志清理），粒度继承 `permissions.yaml` 的风险分级。
- 存储：状态库新增 `approval_grants` 表
  （pattern / granted_by / granted_at / note / revoked_at），
  可审计、可撤销；不用静态配置文件，因为授权是运行时行为。
- 判定顺序：`auto_approve` 规则 → `approval_grants`（未撤销）→
  都不命中才升级问你。
- 每次 grant 命中自动放行都写事件（`task.auto_approved`，
  带 grant_id），Web UI 审批中心可见、可一键撤销
  （`agentctl grant list / revoke`）。
- 安全约束：grant 只能由你本人在对话/Web UI 中创建，Worker 和
  hermes 自身无权写入；删除/外部发布类操作**永不进入 grant 白名单**
  （在 permissions.yaml 里标 `never_grant`）。

### 6.3 超时问题：担心成立，必须先做 A2A 异步化

**现状**：`server_common.py` 里 `message/send` 是**同步阻塞到任务完成**
（代码注释标注了 "PoC 简化"）。长任务会顶到 gateway 的 900s
requestTimeout 或客户端超时——表现就是你说的「等太久中断、拿不到结果」。

**方案**（事件面已就位，改动收敛在 adapter 壳与 hermes 等待逻辑）：

```text
message/send → 立即返回 task(working)      # HTTP 调用秒级结束
runner 后台执行（几小时也行）
完成/失败 → NATS 事件 → StateWriter → SQLite   # 已有链路
hermes wait_task → 订阅事件 / 轮询 SQLite      # 不再挂 HTTP
```

- 断线兜底已有：Worker 失联 → 租约过期 → janitor 按策略处理；
  hermes 重启 → recovery 对齐未完成任务。
- 这是 v3 的**第一优先项**（M1），hermes-brain 建立在其上。

## 7. 里程碑与验收

| 里程碑 | 内容 | 验收 | 状态 |
|---|---|---|---|
| M1 | A2A 异步化 + hermes-brain 最小可用（CLI chat + 审批策略 + 常驻授权） | 长任务（>20min 模拟）不中断；你的 ghcr 部署例子端到端走通；对话内批准/拒绝/常驻授权三态生效 | ✅ 已完成（`f2005ec` + `49b191f`） |
| M2 | env 统一 + 去 Keychain + Dockerfile + compose + 推 agentHub 仓库 + GH workflow 推 ghcr | 干净机器 `docker compose up` 起全系统；ghcr 有镜像 | ✅ 已完成（`a7351c7`–`847a3e4`；ghcr 多架构镜像流水线两次构建成功） |
| M3 | PostgreSQL 双后端 | 离线套件对 SQLite 与 PG 双绿 | ✅ 已完成（`b09e821`；`LAS_DATABASE_URL` 切换，双迁移目录） |
| M4 | OTel + Web UI | Jaeger 见全链路 trace；Web UI 完成看板/审批/事件流 | ✅ 已完成（`27cf691`；Jaeger 单 trace 含 hermes 三跨度 + adapter 跨度） |

M2 的 codex CLI 镜像内分发风险在第一天验证，若不可行立即降级为
混合拓扑并回报。→ 已按决策落地：worker agent **不打包进镜像**，宿主机自装后经心跳注册（M2-2）。

## 8. 实施后状态（2026-08-17 收尾）

- 测试基线：`pytest` **110 passed, 10 skipped**（10 项 skip 为需要真实外部服务的集成用例）。
- 已实测的端到端链路：容器控制面 + 宿主机 fake worker，经心跳注册 → hermes 委派 → A2A 下发 → 执行回报 → hermes 复审，任务 `completed`；Jaeger 可见单 trace 全跨度。
- 已知留白（后续按需推进，不阻塞当前使用）：
  1. codex / kimi **真实 adapter** 尚未接入 compose 栈做过全栈冒烟（仅 fake worker 验证过）；gateway 路由表目前只静态映射 codex/kimi，动态 agent 名的委派需设 `LAS_GATEWAY_URL=` 直连或扩展 gateway 动态路由。
  2. Web UI 为单页看板（任务/审批/事件流），hermes chat 尚未接入 Web UI，对话入口为 `docker compose run --rm agentctl chat`。
  3. OTel 埋点覆盖 hermes / adapter / llm；gateway、state-writer、janitor 的 span 未埋。
  4. 本机开发密钥仍在 macOS Keychain（`agent-system/cliproxy-api-key`），容器部署走 `LAS_LLM_API_KEY` 环境变量，两者互不影响。
