# Hermes 主控的本地多 Agent 协作系统设计文档（v2）

**文档用途**：交付本地 Hermes / Codex 作为实施依据
**目标环境**：macOS，本地运行 Hermes、Codex / ChatGPT、Kimi、DSH 及其他 Agent
**主控 Agent**：Hermes
**设计原则**：Hermes 负责规划与决策；A2A 负责 Agent 间通信；NATS JetStream 负责事件与可靠任务消息；MCP 负责工具调用；SQLite 负责当前状态；共享 Workspace / Git 负责工件；Hindsight 负责长期记忆。

## v2 修订记录（2026-08-17）

v2 在 v1 基础上根据评审意见修订，**不改变组件职责边界**，主要变更：

1. **修正 Phase 顺序矛盾**：v1 第 10 节 Codex Adapter 流程包含 `update SQLite`，但 SQLite 在 Phase 3 才引入。v2 明确：Phase 1 使用 Fake Worker 且不落库；Adapter 永不直接写 SQLite，只发事件（见 §9、§10、§20）。
2. **SQLite 单一写者规则**：明确 `tasks` 等核心表的写入职责划分，消除 Hermes 与 State Writer 双写竞争（见 §22.3）。
3. **状态机补全**：增加 `cancelled` / `retry_pending` / `rejected` 状态，新增合法状态迁移表与 A2A 协议状态映射表（见 §5）。
4. **新增恢复与对账设计**：Hermes 启动恢复流程、Worker 心跳 / 租约、Janitor 对账组件；明确 SQLite 是唯一事实源（见 §17）。
5. **Task 模型字段补全**：`schema_version` / `timeout_seconds` / `max_retries` / `depends_on` / `idempotency_key` / `review`（见 §5）。
6. **记忆层接口契约与选型评估**：定义 Memory Service 抽象接口，Hindsight 保留为默认实现，附备选方案对比（见 §15.3、附录 B）。
7. **NATS Subject 定案**：Phase 1 采用扁平 `task.*`，项目维度放入 envelope payload（见 §7）。
8. **Adapter 并发模型**：默认单任务串行 + 内部队列（见 §9）。
9. **Task ID 并发安全生成规则**（见 §22.1）。
10. **新增端到端时序图**（附录 A）、ADR 目录与契约测试目录（§25）、Phase 0 技术验证 spike（§20）。

---

## 1. 背景与目标

当前本机存在多个能力不同的 Agent，它们分别完成开发、资料分析、运维、研究、自动化等工作，但彼此之间缺乏统一的任务委派、状态同步、工件共享和协作机制。

本项目目标是建立一套轻量、可逐步扩展的本地多 Agent 协作系统，使 Hermes 作为唯一主控 Agent，对其他 Agent 进行任务拆分、委派、跟踪和结果汇总。

### 1.1 核心目标

1. Hermes 作为唯一 Orchestrator / Supervisor。
2. 其他 Agent 作为专业 Worker，不再承担全局调度职责。
3. Agent 间优先使用 A2A 协议通信。
4. 使用 NATS + JetStream 作为事件总线与可靠消息层。
5. 使用 MCP 统一工具层，避免每个 Agent 重复实现文件、Git、SSH、Docker 等能力。
6. 使用 SQLite 保存系统“当前状态”，避免把消息队列当数据库使用。
7. 使用共享 Workspace + Git 共享中间产物，而不是同步完整聊天历史。
8. 使用 Hindsight 存储长期事实、偏好、项目决策等长期记忆（置于统一 Memory 接口之后，可替换，见 §15.3）。
9. 权限最小化：不同 Agent 获得不同文件、Shell、网络和凭据权限。
10. 系统第一阶段应可在单台 Mac 上运行，不依赖 Kubernetes。

### 1.2 非目标

第一阶段不实现：

- 重量级 Web Agent Hub 平台。
- Kubernetes / CRD / Controller 架构。
- 复杂分布式一致性。
- 全量聊天记录同步。
- 所有 Agent 一次性完成接入。
- 让多个 Orchestrator 同时争夺控制权。

---

## 2. 总体架构

```text
                           User
                            │
                            ▼
                       ┌─────────┐
                       │ Hermes  │
                       │  Main   │
                       │Orchestr.│
                       └────┬────┘
                            │
                     Planning / Routing
                            │
                           A2A
                            │
                    ┌───────▼────────┐
                    │  agentgateway  │
                    │ Auth / Route   │
                    │ ACL / Retry    │
                    │ Trace / Policy │
                    └───────┬────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
          Codex A2A      Kimi A2A      Ops / Other A2A
              │             │             │
              └─────────────┼─────────────┘
                            │
                           MCP
                            │
              ┌─────────────┼──────────────┐
              │             │              │
          Filesystem       Git          SSH / Docker
              │             │              │
              └─────────────┼──────────────┘
                            │
                   Shared Workspace

──────────────────────── Event Plane ────────────────────────

                      NATS + JetStream
                            │
          ┌─────────────────┼──────────────────┐
          │                 │                  │
      task events      agent events       system events
          │                 │                  │
          └──────────────► Hermes ◄────────────┘
                            │
                ┌───────────┼───────────┐
                ▼           ▼           ▼
          State Writer   Janitor    Observer/UI

──────────────────────── State Plane ────────────────────────

                         SQLite  ◄── 唯一事实源（single source of truth）
                            │
              task / agent / artifact / run
                            │
                        Workspace
                            │
                   Memory Service（接口）
                            │
                        Hindsight
                    Long-term Memory
```

---

## 3. 组件职责

### 3.1 Hermes

Hermes 是系统唯一主控 Agent。

职责：

- 接收用户目标。
- 将目标拆成 Task / Subtask。
- 判断应由哪个 Agent 执行。
- 通过 A2A 向 Worker 发起任务。
- 订阅 NATS 事件了解任务进度。
- 检查结果是否满足要求（Review，见 §5.3）。
- 必要时发起重试、补充、Review 或二次委派。
- 将多个 Worker 的结果合并成最终输出。
- 决定哪些事实进入长期记忆（通过 Memory 接口写入）。
- 危险操作的用户审批入口（见 §18）。

Hermes 不应承担：

- 所有具体代码实现。
- 所有资料分析。
- 所有运维操作。
- 充当消息队列。
- 充当数据库。

---

### 3.2 Worker Agent

示例：

### Codex

```text
role: software_engineering
skills:
  - coding
  - debugging
  - refactor
  - testing
  - git
  - code_review
```

### Kimi

```text
role: research_and_long_context
skills:
  - research
  - long_context
  - document_analysis
  - chinese_content
  - summarization
```

### Ops Agent

```text
role: infrastructure
skills:
  - ssh
  - docker
  - nginx
  - linux
  - diagnostics
```

Worker 原则：

- 接受结构化任务。
- 执行任务。
- 产生 Artifact。
- 返回结构化结果。
- 发布状态事件（**不直接写 SQLite**，见 §22.3）。
- 不自行修改全局任务计划，除非 Hermes 明确授权。

---

### 3.3 A2A

A2A 用于 Agent 与 Agent 之间的“业务语义通信”。

负责：

- Agent Card / 能力发现。
- SendMessage / Task 创建。
- Task 状态。
- Artifact 返回。
- Agent 间上下文传递。

建议：

- Hermes 作为 A2A Client。
- Worker 暴露 A2A Server。
- 不原生支持 A2A 的 App 通过 Adapter 包装。
- 内部状态机与 A2A 协议状态的映射见 §5.4，Adapter 必须按该表实现，不得各自发明。

示例：

```text
Hermes
  │
  ├─ A2A → Codex Adapter → Codex Runtime/API
  ├─ A2A → Kimi Adapter  → Kimi API/Runtime
  └─ A2A → Ops Adapter   → Local Agent
```

---

### 3.4 agentgateway

agentgateway 只作为通信治理层，不作为 Orchestrator。

职责：

- A2A / MCP 请求路由。
- 身份认证。
- 权限控制。
- Retry / Timeout。
- TLS / mTLS。
- Rate Limit。
- Policy。
- Trace / Metrics。

第一阶段先旁路 agentgateway，直接 Hermes → A2A Worker，链路打通后再加入 gateway。

推荐顺序：

```text
Phase 1: Hermes → A2A Worker
Phase 2: Hermes → agentgateway → A2A Worker
```

> **已知风险（显式接受）**：Phase 1–4 所有服务监听 `127.0.0.1` 且无鉴权，即**任何本机进程都可以向 Adapter 提交任务**。这在单人单 Mac 场景下可接受，但必须在文档和 README 中显式声明；agentgateway（Phase 5）落地前不得把任何端口绑定到非 loopback 地址。

---

### 3.5 NATS + JetStream

NATS 是消息总线。

NATS Core 负责：

- Publish / Subscribe。
- Request / Reply。
- Subject 路由。

JetStream 增加：

- 消息持久化。
- ACK。
- Durable Consumer。
- 消息重放。
- Work Queue。
- Worker 离线后恢复消费。

在本系统中，NATS 用于：

> 传播“系统发生了什么”，而不是保存系统最终状态。

> **事实源声明**：SQLite 是系统状态的唯一事实源（single source of truth）。JetStream 是传输与暂存层，保留期（7–30 天）短于任务可能的生命周期。任何情况下 SQLite 与 JetStream 中的信息冲突时，以 SQLite 为准；长期对账由 Janitor 负责（见 §17.5）。

### 不建议

```text
Hermes → NATS → 塞完整 A2A 协议报文 → Worker
```

作为第一阶段的主要调用方式。

### 建议

```text
Hermes ──A2A──► Worker

Hermes / Worker ──publish──► NATS
                             │
                             ├─ task.started
                             ├─ task.progress
                             ├─ task.completed
                             └─ task.failed
```

---

### 3.6 SQLite

SQLite 保存系统当前状态，是**唯一事实源**。

NATS = Event
SQLite = State

必须能够通过 SQLite 直接查询：

- 当前有哪些 Agent。
- 当前有哪些 Task。
- Task 属于哪个 Parent Task。
- Task 当前状态。
- 谁是 Owner。
- 任务开始 / 完成时间。
- Artifact 在哪里。
- 是否需要重试。
- 最后错误是什么。

写入规则见 §22.3（单一写者原则）。

第一阶段使用 SQLite 足够。

未来多机部署时再迁移 PostgreSQL。

---

### 3.7 MCP

MCP 用于 Agent 调用工具。

建议共享的 MCP Server：

```text
filesystem
Git
shell
browser
Docker
SSH
memory（Memory 接口的 MCP 封装）
project-state
notification
```

原则：

- Agent 间协作：A2A。
- Agent 调工具：MCP。
- 不用 MCP 模拟完整 Agent-to-Agent 协议。

---

### 3.8 Shared Workspace

推荐目录：

```text
~/AgentWorkspace/
├── config/
│   ├── agents.yaml
│   ├── permissions.yaml
│   └── subjects.yaml
├── projects/
│   └── <project>/
├── tasks/
│   └── <task-id>/
│       ├── task.yaml
│       ├── context.md
│       ├── status.json
│       ├── input/
│       ├── artifacts/
│       └── logs/
├── runtime/
│   ├── agent-state.db
│   └── pids/
├── logs/
└── scripts/
```

Task 示例：

```text
~/AgentWorkspace/tasks/T-20260817-001/
├── task.yaml
├── context.md
├── status.json
├── input/
├── artifacts/
│   ├── implementation.md
│   ├── patch.diff
│   ├── test-result.txt
│   └── review.md
└── logs/
    └── adapter.jsonl      # 每个任务自己的 JSONL 日志（见 §16）
```

核心原则：

> 共享 Artifact，不同步完整 Conversation。

---

## 4. Agent Registry

第一阶段不开发独立 Registry Server。

使用：

```text
config/agents.yaml
+
SQLite agents 表
```

示例：

```yaml
agents:
  hermes:
    id: hermes
    role: orchestrator
    enabled: true
    endpoint: http://127.0.0.1:8100
    protocol: a2a
    max_concurrent_tasks: 4        # Hermes 自身可并行跟踪的任务数
    skills:
      - planning
      - routing
      - browser
      - shell

  codex:
    id: codex
    role: worker
    enabled: true
    endpoint: http://127.0.0.1:8201
    protocol: a2a
    max_concurrent_tasks: 1        # Worker 并发上限，见 §9.1
    skills:
      - coding
      - testing
      - debugging
      - git
      - code_review

  kimi:
    id: kimi
    role: worker
    enabled: true
    endpoint: http://127.0.0.1:8202
    protocol: a2a
    max_concurrent_tasks: 2
    skills:
      - research
      - long_context
      - document_analysis
```

注意：Agent Card 对外暴露的 skills 不得超出 permissions.yaml 实际授权的范围（能力与权限必须一致，避免 Hermes 路由到一个实际无权执行的任务）。

后续可由 Agent Card 自动发现并同步 Registry。

---

## 5. Task 数据模型

### 5.1 统一任务模型（v2 补全字段）

```yaml
schema_version: 1                  # 任务格式版本，演进时 +1 并写 migration

id: T-20260817-001
parent_id: null
root_id: T-20260817-001
project: multi-agent-platform

created_by: hermes
assigned_to: codex

status: queued
priority: normal

objective: >
  Implement the initial A2A worker adapter.

context_files:
  - context.md

depends_on: []                     # 前置任务 ID 列表；全部 accepted 后才可 queued

artifacts_expected:
  - implementation.md
  - source_code
  - test_result

constraints:
  - do_not_modify_global_config
  - run_tests_before_complete

timeout_seconds: 1800              # 单次 attempt 的执行超时
max_retries: 2                     # 重试上限；retry_count 达到后转 failed 终态
idempotency_key: T-20260817-001:1  # task_id + attempt，见 §22.5

review:                            # completed 后由 Hermes 填写
  reviewer: null                   # hermes | user
  verdict: null                    # approved | rejected
  notes: null

created_at: 2026-08-17T11:30:00+08:00
started_at: null
completed_at: null
```

### 5.2 状态机（v2 补全）

```text
created
   │
   ▼
queued ◄────────────────────┐
   │                        │
   ▼                        │
assigned                     │
   │                         │
   ▼                         │
working ──timeout──► failed ─┤ (retry_count < max_retries)
   │                  │      │   经 retry_pending 回到 queued
   ├────► blocked     │      │
   │       │  │       ▼      │
   │       │  │   retry_pending
   │       │  │       │
   │◄──────┘  └───────┘
   │  (恢复/重试)
   ▼
completed
   │
   ▼
reviewed ──verdict=rejected──► working（返工，attempt +1）
   │
   │ verdict=approved
   ▼
accepted（终态）

任意非终态 ──► cancelled（终态；用户取消 / 审批拒绝 / 父任务取消级联）
failed 且 retry_count >= max_retries ──► failed 终态（不再自动流转）
```

终态：`accepted`、`failed`（重试耗尽）、`cancelled`。

### 5.3 合法状态迁移表

| from | to | 触发者 | 条件 |
|---|---|---|---|
| created | queued | Hermes | depends_on 全部 accepted |
| queued | assigned | Hermes | 路由完成，Worker 健康 |
| assigned | working | Worker/Adapter | A2A task 受理 |
| working | blocked | Worker | 需要审批 / 缺输入 / 外部依赖 |
| blocked | working | Hermes | 审批通过 / 输入补齐 |
| blocked | failed | Hermes | 审批拒绝且不可继续 |
| blocked | cancelled | Hermes/用户 | 取消 |
| working | completed | Worker | 产出全部 artifacts_expected |
| working | failed | Worker/超时看门狗 | 执行失败或超过 timeout_seconds |
| failed | retry_pending | Hermes | retry_count < max_retries |
| retry_pending | queued | Hermes | 退避结束（exponential backoff） |
| completed | reviewed | Hermes | review 完成，填写 review.verdict |
| reviewed | accepted | Hermes/用户 | verdict = approved |
| reviewed | working | Hermes | verdict = rejected，返工（attempt +1） |
| 任意非终态 | cancelled | Hermes/用户 | 取消；父任务取消时级联到子任务 |

非法迁移（如 State Writer 收到迟到的 `task.progress` 而任务已 `cancelled`）必须被拒绝、记入 `system.audit` 事件，**不得**覆盖现有状态（见 §22.3）。

### 5.4 内部状态 ↔ A2A 协议状态映射

Adapter 对外只暴露 A2A 标准状态，按下表映射，不得自行扩展 A2A 状态：

| 内部状态 | A2A TaskState | 说明 |
|---|---|---|
| created / queued / assigned | `submitted` | Hermes 侧准备阶段 |
| working / retry_pending | `working` | |
| blocked（reason=approval_required） | `input-required` | message 中携带审批事项 |
| blocked（其他原因） | `input-required` | message 中携带所需输入 |
| completed / reviewed / accepted | `completed` | review 结论是内部概念，放入 Artifact metadata |
| failed | `failed` | |
| cancelled | `canceled` | |

---

## 6. SQLite Schema

第一版建议至少包含以下表。

### 6.1 agents

```sql
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    endpoint TEXT,
    protocol TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    skills_json TEXT,
    max_concurrent_tasks INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT,
    lease_expires_at TEXT,          -- 心跳租约，见 §17.4
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### 6.2 tasks

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    parent_id TEXT,
    root_id TEXT,
    project TEXT,
    created_by TEXT,
    assigned_to TEXT,
    status TEXT NOT NULL,
    priority TEXT,
    objective TEXT NOT NULL,
    depends_on_json TEXT,           -- JSON array of task ids
    constraints_json TEXT,
    timeout_seconds INTEGER,
    max_retries INTEGER NOT NULL DEFAULT 2,
    idempotency_key TEXT UNIQUE,
    result_summary TEXT,
    error_message TEXT,
    review_json TEXT,               -- {reviewer, verdict, notes}
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
```

### 6.3 artifacts

```sql
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_id TEXT,
    type TEXT,
    name TEXT,
    path TEXT,
    sha256 TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
```

### 6.4 task_runs

```sql
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT,
    trace_id TEXT
);
```

### 6.5 events

可选保存最近事件索引，不作为完整事件源：

```sql
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    task_id TEXT,
    agent_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);
```

### 6.6 counters（v2 新增）

Task ID 每日序列的并发安全来源（见 §22.1）：

```sql
CREATE TABLE counters (
    name TEXT PRIMARY KEY,          -- 例: task:20260817
    value INTEGER NOT NULL DEFAULT 0
);
```

---

## 7. NATS Subject 设计

### 7.1 Subject 定案（v2）

**Phase 1 采用扁平 `task.*` subject，项目维度放入事件 envelope 的 `payload.project`，不引入 `project.<id>.task.*` 层级。**

理由：Subject 一旦发布并被 consumer 绑定，修改成本很高；扁平结构配合 payload 过滤已能满足单项目和小规模多项目场景。未来确需按项目隔离路由 / 权限时，再新增带项目维度的 subject 并让 Stream 同时绑定新旧 subject 平滑过渡。

```text
agent.<agent_id>.status
agent.<agent_id>.heartbeat
agent.<agent_id>.event

task.created
task.assigned
task.started
task.progress
task.blocked
task.completed
task.failed
task.reviewed
task.accepted
task.cancelled

artifact.created
artifact.updated

system.alert
system.audit
```

不要过早把 subject 设计得过深。

### 7.2 事件格式

统一 envelope：

```json
{
  "event_id": "E-uuid",
  "event_type": "task.completed",
  "timestamp": "2026-08-17T11:35:00+08:00",
  "source": "codex",
  "task_id": "T-20260817-001",
  "trace_id": "trace-uuid",
  "payload": {
    "project": "multi-agent-platform",
    "summary": "A2A adapter implemented",
    "status_from": "working",
    "status_to": "completed",
    "attempt": 1,
    "artifacts": [
      "artifacts/implementation.md"
    ]
  }
}
```

注意：

- `payload.status_from` / `status_to` 用于 State Writer 做迁移合法性校验（§5.3）。
- `payload.attempt` 与 `idempotency_key` 配合去重（§22.5）。
- 心跳事件 `agent.<id>.heartbeat` 的 payload 只需 `{ "lease_ttl_seconds": 90 }`。

---

## 8. JetStream 建议

建议建立几个 Stream，而不是一个 Subject 一个 Stream。

### AGENT_EVENTS

```text
Subjects:
  agent.*.*
  task.*
  artifact.*
  system.*

Retention:
  LimitsPolicy

Storage:
  File
```

第一阶段保留 7~30 天即可（再次强调：保留期短于任务生命周期，因此 SQLite 才是事实源，见 §3.5）。

### Durable Consumers

建议：

```text
hermes-orchestrator
state-writer
janitor
observer
```

其中：

- `hermes-orchestrator`：Hermes 监听任务状态。
- `state-writer`：将关键事件按迁移表校验后写入 SQLite（唯一事件写库者，见 §22.3）。
- `janitor`：对账与恢复（见 §17.5）。
- `observer`：供未来 UI / CLI 使用。

---

## 9. A2A Adapter 设计

不是所有桌面 Agent 都原生支持 A2A，因此需要 Adapter。

统一结构：

```text
A2A Request
    │
    ▼
┌──────────────┐
│ Agent Adapter│
├──────────────┤
│ A2A Server   │
│ Task Queue   │   # 内部 FIFO 队列，见 §9.1
│ Mapper       │
│ Runtime/API  │
│ Event Pub    │
└──────┬───────┘
       │
       ▼
 Actual Agent
```

Adapter 职责：

1. 暴露 Agent Card。
2. 接收 A2A Task / Message。
3. 转换为对应 Agent 的 CLI / API / SDK 调用。
4. 写入 Task Workspace。
5. 发布任务状态到 NATS（**Adapter 不直接写 SQLite**，状态持久化由 State Writer 完成）。
6. 收集 Artifact。
7. 将结果映射回 A2A Artifact / Task Status（按 §5.4 映射表）。
8. 定期发送心跳（见 §17.4）。

### 9.1 Adapter 并发模型（v2 新增）

- 每个 Adapter 默认 `max_concurrent_tasks = 1`（单任务串行），在 agents.yaml 中可按 Agent 调整（见 §4）。
- 超出并发的 A2A 请求进入 Adapter 内部 FIFO 队列，A2A 状态保持 `submitted`，队首任务完成后依次受理。
- 理由：本机 Worker（尤其是桌面 Agent / CLI 包装）大多无法安全并发；串行默认可避免上下文互相污染。确认某 Worker 支持并发后再上调。

---

## 10. Codex Adapter 第一版

Codex 是最适合作为第一个 Worker PoC 的 Agent。

建议接口：

```text
POST /a2a
GET  /.well-known/agent-card.json
GET  /health
```

执行流程（v2 修正：移除 Adapter 直写 SQLite，改为一律发事件）：

```text
Hermes
  │
  └─ A2A task
       │
       ▼
 Codex Adapter
       │
       ├─ create task workspace
       ├─ write context.md
       ├─ publish task.started            # State Writer 落库: working
       ├─ invoke local Codex
       ├─ collect output
       ├─ save artifacts
       ├─ publish artifact.created
       ├─ publish task.completed          # State Writer 落库: completed
       └─ return A2A artifact
```

说明：

- 在 Phase 1–2（SQLite 尚未引入），Adapter 只维护内存态 A2A Task Store + 发事件；事件无人消费也允许丢失，因为此阶段不做状态恢复。
- 从 Phase 3 起，事件由 State Writer 消费落库，SQLite 成为事实源。
- Codex Adapter 不应直接访问 Hermes 的长期记忆。
- Hermes 应主动把当前任务所需上下文写入 `context.md`。

---

## 11. Hermes Orchestrator Integration

Hermes 侧需要增加一个轻量集成层，而不是重写 Hermes。

建议实现：

```text
hermes-orchestrator/
├── registry.py
├── task_manager.py
├── a2a_client.py
├── nats_client.py
├── state_store.py
├── artifact_manager.py
├── memory_client.py      # Memory 接口客户端，见 §15.3
├── recovery.py           # 启动恢复与对账调用，见 §17
└── policy.py
```

能力：

### registry.py

- 加载 `agents.yaml`。
- 查找 Agent。
- 按 skill 过滤候选 Agent。
- 读取 `max_concurrent_tasks`，路由时跳过队列已满的 Agent。

### task_manager.py

- 创建 Task（含并发安全 ID 生成，见 §22.1）。
- 创建 Workspace。
- Task 状态机（只允许 §5.3 表中的迁移）。
- Parent / Child Task 与 depends_on 解析。
- 级联取消。

### a2a_client.py

- Agent Card 获取。
- A2A SendMessage。
- Task 查询（恢复流程依赖，见 §17.2）。
- Artifact 获取。

### nats_client.py

- Publish event。
- Subscribe event。
- JetStream ACK。

### state_store.py

- SQLite CRUD。
- Migration。

### artifact_manager.py

- Artifact 路径。
- SHA256。
- Metadata。

### memory_client.py

- 调用 Memory 接口（retain / recall / reflect）。
- Hermes 是唯一写方；Worker 无访问凭据。

### recovery.py

- Hermes 启动时扫描未完成（非终态）任务。
- 对每个 working/assigned 任务，通过 A2A 按 task_id 查询 Worker 侧真实状态并对齐（见 §17.2）。

### policy.py

- Agent 权限。
- Task 类型到 Agent 的约束。
- Dangerous Operation 审批规则。

---

## 12. 路由策略

第一阶段不要让 LLM 完全自由路由。

采用：

```text
Hard Rule
   ↓
Capability Match
   ↓
Capacity Check（max_concurrent_tasks / 当前负载）
   ↓
Hermes Reasoning
```

示例：

```yaml
routing:
  coding:
    preferred:
      - codex

  code_review:
    preferred:
      - codex

  research:
    preferred:
      - kimi

  infrastructure:
    preferred:
      - ops
```

Hermes 可以覆盖默认规则，但必须记录理由（写入 task 的 metadata 或 ADR）。

---

## 13. 权限模型

不要让所有 Agent 拥有相同权限。

### Hermes

```text
filesystem: rw workspace
shell: limited
browser: rw
nats: pub/sub
sqlite: rw（限 §22.3 规定的字段与迁移）
memory: rw（系统内唯一长期记忆写方）
A2A: client
```

### Codex

```text
filesystem: rw assigned project only
git: rw assigned repo
shell: sandbox / project scope
nats: publish task/artifact events
sqlite: no direct write（禁止；经事件间接落库）
memory: no direct access
ssh: denied by default
```

### Kimi

```text
filesystem: read task context
artifact: write own task artifacts
shell: denied
ssh: denied
nats: publish task events
memory: no direct access
```

### Ops Agent

```text
filesystem: limited
ssh: allowed to configured hosts
docker: allowed
secrets: named references only
personal_documents: denied
```

> 再次提示：以上权限在 Phase 1–4 依赖进程自觉 + 配置约束；强制性隔离（gateway ACL）在 Phase 5 才落地。此前所有端口必须绑定 loopback（见 §3.4 风险声明）。

---

## 14. Secrets 管理

不要把密码/API Key 写进：

```text
agents.yaml
task.yaml
context.md
Git
NATS payload
```

第一阶段使用：

```text
macOS Keychain
+
environment variable references
```

例如：

```yaml
credentials:
  kimi_api_key: keychain://agent-system/kimi-api-key
```

Adapter 在运行时读取。

---

## 15. Memory 设计

采用三层记忆。

### 15.1 Task Memory

位置：

```text
tasks/<id>/context.md
```

生命周期：单个 Task。

### 15.2 Project Memory

位置：

```text
projects/<project>/docs/
Git
Markdown
```

内容：

- 架构决策。
- API。
- 约束。
- TODO。
- Known Issues。

### 15.3 Long-term Memory（v2：接口契约 + 选型结论）

#### 接口抽象

长期记忆不直接绑定具体实现，所有访问经过统一 Memory Service 接口（Python 客户端 + MCP 封装）：

```python
class MemoryService(Protocol):
    def retain(self, content: str, scope: str, metadata: dict) -> str:
        """写入一条长期记忆。scope 例: user / project:<id> / system。返回 memory_id。"""

    def recall(self, query: str, scope: str | None = None,
               budget_tokens: int = 2048) -> list[Memory]:
        """按相关性召回，带 token 预算。返回带来源与时间戳的记忆列表。"""

    def reflect(self, topic: str) -> str | None:
        """可选：对某主题做归纳（mental model）。实现不支持时返回 None。"""
```

写入规则（与 v1 一致）：

- 由 Hermes 决定写入：长期用户偏好、稳定环境事实、项目长期决策、重要历史结论。
- Worker 不直接写长期记忆（无凭据）。
- Worker 如需"建议记住"，通过 `task.completed` 的 `payload.memory_hints` 提交，由 Hermes 裁决。

#### 选型结论（2026-08 评估）

**默认实现保持 Hindsight（Vectorize，MIT 开源），置于上述接口之后。**

理由：

- 部署形状与本系统匹配：单容器、内嵌 PostgreSQL、无外部数据库依赖，符合"单台 Mac、轻量、渐进"的第一阶段约束；提供 `retain / recall / reflect` 三个操作，与接口几乎一一对应。
- MIT 许可，可自托管，提供 MCP 集成路径。
- 公开评测（LongMemEval / BEAM）处于第一梯队，且 recall 路径无额外 LLM 调用，本地运行成本低。

已知限制（显式接受）：

- 无内建 RBAC / ABAC，仅单个静态 API Key——本设计中 Hermes 是唯一写方，多租户隔离非目标，可接受。
- 项目较新（2025-12 发布），生态小于 Mem0——通过接口抽象保留替换能力来对冲。

备选对比详见附录 B。**替换触发条件**：Hindsight 停止维护、出现阻塞性缺陷、或未来需要强时序图查询时，切换到 Mem0 或 Graphiti，业务代码只改 Memory 客户端实现。

---

## 16. Observability

第一阶段必须保留：

- task_id
- root_id
- parent_id
- trace_id
- agent_id
- attempt
- timestamps

日志格式尽量 JSONL。除集中日志外，每个任务在 `tasks/<id>/logs/adapter.jsonl` 保留自己的执行日志，便于单任务排查。

示例：

```json
{
  "ts": "2026-08-17T11:40:00+08:00",
  "level": "INFO",
  "trace_id": "trace-123",
  "task_id": "T-001",
  "agent_id": "codex",
  "event": "task.completed"
}
```

后续可接 OpenTelemetry。

---

## 17. 故障处理、恢复与对账（v2 大幅扩充）

### 17.1 Agent 不在线

```text
A2A health fail
→ task remains queued
→ publish agent.offline
→ Hermes chooses wait / fallback
```

### 17.2 Hermes 重启恢复流程（v2 新增，必须实现）

Hermes 集成层启动时，在接收新任务之前执行：

```text
1. 连接 SQLite，查询所有非终态任务（status NOT IN accepted/failed终态/cancelled）
2. 对每个 status IN (assigned, working) 的任务：
   a. 通过 A2A 按 task_id 查询 Worker 侧真实状态
   b. Worker 返回状态 → 按 §5.4 映射回内部状态，对齐 SQLite
   c. Worker 不可达 → 检查该 Agent 租约（§17.4）：
      - 租约未过期 → 保持现状，等待
      - 租约已过期 → 按 §17.4 失联策略处理
3. 对每个 status = queued/retry_pending 的任务：重新进入调度
4. 恢复 JetStream durable consumer（hermes-orchestrator），从 last ack 继续
5. 完成后才开放新任务入口
```

### 17.3 Worker 超时

```text
A2A timeout（超过 task.timeout_seconds）
→ task status = failed（attempt 记为失败）
→ retry_count + 1
→ 若 retry_count < max_retries：retry_pending → exponential backoff → queued
→ 否则：failed 终态，publish task.failed，通知用户
```

超时判定由 Adapter 侧看门狗执行（Adapter 最清楚 Worker 是否还在跑），并发布 `task.failed`（reason=timeout）。

### 17.4 Worker 心跳与租约（v2 新增）

- 每个 Adapter 每 **30 秒**发布 `agent.<id>.heartbeat`，payload 含 `lease_ttl_seconds: 90`。
- `agents.yaml.enabled` 是初始目标状态；管理员通过 WebUI 设置的
  `agent_controls.enabled` 是运行时目标状态并覆盖静态值，二者都优先于心跳观测状态。
- State Writer 仅对已启用或动态新增的 Agent 更新 `agents.last_seen_at` 与
  `agents.lease_expires_at = now + lease_ttl`；已停用 Agent 的心跳只保留审计事件，
  不注册、不续租、不发现能力、不绑定 Profile。
- 运行时停用会立即使旧租约失效；重新启用只进入 `probing/等待注册`，不得复用旧租约，
  必须由新心跳完成注册后才可参与计划和委派。每次开关变更写入审计事件。
- Hermes 对停用 Agent 的建计划、委派与批准路径统一返回
  `needs_confirmation / agent_disabled`；必须询问用户是否先启用并重新探测，
  或改派其他已启用 Agent，禁止静默改派。
- 失联判定：`now > lease_expires_at` 即失联。
- 失联时该 Agent 名下 working 任务的处理策略（agents.yaml 可配，默认）：

```yaml
on_lease_expired: requeue   # requeue: 回到 queued 等待重派（默认，适合幂等任务）
                            # fail: 直接 failed 走重试流程
```

幂等性由 `idempotency_key = task_id + attempt` 保证：requeue 产生新 attempt，Worker 侧如收到同 key 请求必须去重。

### 17.5 Janitor 对账组件（v2 新增）

独立进程 `ai.janitor`，每 60 秒扫描一次 SQLite：

```text
1. working 任务但所属 Agent 租约过期 → 按 §17.4 策略 requeue / failed
2. working 任务的 task_runs.started_at 距今 > timeout_seconds 且无对应事件
   → 标记 failed(reason=timeout_swept)，走重试流程
3. queued/retry_pending 任务的 depends_on 已满足 → 推进调度
4. parent 已 cancelled 但 child 仍非终态 → 级联取消
5. artifacts 表记录的文件在磁盘缺失 → system.alert（不自动删记录）
```

Janitor 只依据 SQLite 工作，不消费业务事件流；它是"事实源驱动"的最后兜底。

### 17.6 Worker 完成但事件丢失

依靠 JetStream ACK / durable consumer。Adapter 必须在 State Writer ACK 之前能安全重发：同一事件以 `event_id` 去重（State Writer 记录已处理 event_id）。

### 17.7 NATS 暂时不可用

核心 A2A 调用可以继续。Adapter 本地暂存事件（`tasks/<id>/logs/events-pending.jsonl`），NATS 恢复后重发。

### 17.8 SQLite 被锁

- WAL 模式。
- 单独 State Writer 优先。
- Worker 一律不直接写 DB（见 §22.3）。

推荐：

```sql
PRAGMA journal_mode=WAL;
```

---

## 18. 用户审批点

危险操作必须要求用户审批，例如：

```text
rm / destructive filesystem
Git force push
生产环境 SSH
Docker prune
数据库 DDL / delete
凭据变更
外部发布
付款 / 购买
```

Hermes 负责审批决策入口。

Worker 返回（A2A 侧表现为 `input-required`，见 §5.4）：

```text
status: blocked
reason: approval_required
```

Hermes 再向用户请求确认。用户拒绝时，任务转 `failed` 或 `cancelled`（由用户选择），不是停留在 blocked。

---

## 19. 推荐技术栈

第一阶段：

```text
Language:        Python 3.12+
HTTP:            FastAPI
A2A SDK:         官方兼容 SDK / 协议实现
Message Bus:     NATS + JetStream
State:           SQLite + WAL
Config:          YAML
Workspace:       Filesystem
Artifacts:       Filesystem + Git
Secrets:         macOS Keychain
Memory:          Memory 接口 + Hindsight（可替换，见 §15.3）
Gateway:         agentgateway（Phase 5）
Process Manager: launchd
```

> **前置 Spike（Phase 0 必做）**：验证 Hermes 现有插件 / 扩展机制，确定集成层语言。如果 Hermes 插件机制更适合 TypeScript，可只让 Adapter 使用 Python，Hermes 集成保持其原生语言。该结论必须写成 ADR-0001 后再进入 Phase 1。

---

# 20. 实施阶段（v2 修正顺序，与 §27 对齐）

## Phase 0：环境准备 + 技术验证

目标：建立基础目录、运行依赖，并完成两个关键 Spike。

任务：

- [ ] 创建 `~/AgentWorkspace`。
- [ ] 初始化 Git repo（含 `docs/adr/` 目录）。
- [ ] 创建 Python venv。
- [ ] 启动 NATS + JetStream。
- [ ] 建立 `agents.yaml`（含 `max_concurrent_tasks`）。
- [ ] 建立基础 CLI 骨架。
- [ ] **Spike 1**：Hermes 插件机制验证 → 集成层语言决策 → ADR-0001。
- [ ] **Spike 2**：Hindsight 本地起容器，跑通 retain/recall 最小用例 → ADR-0002。
- [ ] （SQLite 建库提前到本阶段，为 Phase 1 后的所有阶段铺路——成本极低，消除 v1 的顺序矛盾。）

验收：

```text
nats server healthy
SQLite writable（WAL 模式）
workspace initialized
ADR-0001 / ADR-0002 已提交
```

---

## Phase 1：A2A Fake Worker PoC

目标：打通 Hermes → A2A → Adapter 全链路，**不接真实 Codex，不依赖 SQLite 状态**。

实现：

```text
Hermes test client
   │
   └──── A2A ────► Fake Worker Adapter
                        │
                        ├─ receive task
                        ├─ sleep 1s
                        ├─ create artifact
                        ├─ publish task.completed（允许无人消费）
                        └─ return result
```

任务：

- [ ] 实现 Agent Card。
- [ ] 实现 `/health`。
- [ ] 实现最小 A2A Task 接收（含内部 FIFO 队列，§9.1）。
- [ ] 保存 Artifact 到 task workspace。
- [ ] 返回任务结果。
- [ ] 状态按 §5.4 映射表暴露。

验收场景：

Hermes 发出：

```text
Create hello.py and add a unit test.
```

Fake Worker 返回结构化 Artifact，Hermes test client 可以读取结果。

---

## Phase 2：接入真实 Codex + NATS Event Plane

目标：Fake Worker 的运行时替换为真实 Codex；任务过程不再依靠 Hermes 轮询。

任务：

- [ ] Adapter 调用本地 Codex（替换 fake runtime）。
- [ ] Adapter publish `task.started` / `task.progress` / `task.completed` / `task.failed`。
- [ ] Hermes durable consumer。
- [ ] 事件本地暂存与重发（§17.7）。

验收：

```text
Codex 真实完成 §21 的 hello.py 场景
NATS 暂时中断 → 事件暂存 → 恢复后重放
Hermes restart → resume consumer
```

---

## Phase 3：SQLite State Plane

目标：任意时刻可恢复全局任务状态；SQLite 成为唯一事实源。

任务：

- [ ] migrations（含 counters 表）。
- [ ] agents / tasks / task_runs / artifacts 表。
- [ ] State Writer consumer（唯一事件写库者，含迁移合法性校验与 event_id 去重）。
- [ ] Task ID 改走 counters 表生成（§22.1）。

验收：

执行：

```text
agentctl task list
agentctl task show T-xxx
agentctl agent list
```

可以看到准确状态。手工构造一条非法迁移事件（如 cancelled 后补发 progress），State Writer 拒绝并产生 `system.audit`。

---

## Phase 4：Hermes Orchestrator Integration

目标：Hermes 正式成为任务控制器，并具备完整恢复能力。

任务：

- [ ] `create_task()` / `delegate_task()` / `wait_task()`（event-driven）。
- [ ] `review_result()`（completed → reviewed → accepted / rejected 返工）。
- [ ] `retry_task()` / `cancel_task()`（含级联取消）。
- [ ] `list_agents()` / `find_agent_by_skill()`（含容量检查）。
- [ ] **启动恢复流程（§17.2）**。
- [ ] **Janitor 对账进程（§17.5）**。
- [ ] 心跳与租约（§17.4）。

验收：

用户给 Hermes 一个开发任务，Hermes 可以：

```text
分析
→ 创建子任务
→ 委派 Codex
→ 等待完成
→ Review
→ 要求修复（rejected → working 返工）
→ 接收最终结果
→ 汇报用户
```

并且：任务执行中杀掉 Hermes / Codex 进程再重启，任务不丢失、状态可对齐（§17.2 / §17.4）。

---

## Phase 5：agentgateway

目标：加入统一通信治理。

任务（2026-08-17 完成，详见 docs/agentgateway.md）：

- [x] 部署 agentgateway。（v1.4.1 darwin-arm64，sha256 校验，infra/agentgateway/）
- [x] Hermes 只访问 gateway。（A2aClient.for_agent：AGENT_GATEWAY_URL 非空时走 gateway + Bearer key）
- [x] Worker 不直接向公网暴露。（全部 loopback；gateway 是唯一认证入口）
- [x] 加入 auth。（gateway 级 apiKey strict，key 存 Keychain 经 env 注入）
- [x] 加入 ACL。（路由级 CEL authorization，按 key 元数据 agents 列表放行，热加载）
- [x] 加入限流。（每条 Agent 路由独立 token bucket，30 次突发 / 每分钟补充 30 次）
- [x] 加入 timeout/retry。（路由级 requestTimeout + retry attempts=2）
- [ ] 加入 tracing。（OTel 后置，见 §29）

验收：

- [x] 禁用某 Agent 权限后，gateway 可以阻止不允许的请求。
  （test_agentgateway.py：无 key 401 / 经 gateway 委派成功 / 禁用后 403）

---

## Phase 6：第二个 Worker

优先选择 Kimi 或当前另一个高频 Agent。

目标：证明系统不是 Codex 专用。

验收任务：

```text
Hermes
 ├─ Kimi: research / analyze docs
 └─ Codex: implement based on research（depends_on 前置任务）
```

最终 Hermes 汇总。

---

## Phase 7：MCP Shared Tool Layer

目标：减少不同 Agent 的工具重复配置。

先接：

```text
filesystem
git
browser
```

再接：

```text
SSH
Docker
memory（Memory 接口的 MCP 封装，仅 Hermes 侧挂载）
```

---

## Phase 8：Observer / CLI

不优先做 Web UI。

先做 CLI：

```bash
agentctl status
agentctl agents
agentctl tasks
agentctl task T-001
agentctl events --follow
agentctl retry T-001
agentctl cancel T-001
```

Web UI 后置。

---

# 21. 第一个完整协作测试

用户任务：

```text
分析某个本地项目的问题并完成修复。
```

预期流程：

```text
User
 │
 ▼
Hermes
 │
 ├─ T001: inspect project
 │
 ├─ T002 → Codex（depends_on: [T001]）
 │          │
 │          ├─ inspect source
 │          ├─ implement fix
 │          ├─ run test
 │          └─ artifact: patch.diff
 │
 ├─ receives task.completed via NATS
 │
 ├─ review artifact（completed → reviewed）
 │
 ├─ if rejected:
 │      └─ T002 返工（working，attempt +1）
 │
 ├─ accepted
 │
 └─ final response → User
```

必须验证：

- Hermes 不需要人工复制 Codex 输出。
- Codex 不需要用户重复输入上下文。
- Task 状态可恢复。
- Agent 重启不会丢失已完成事件。
- Artifact 可以追踪到原始 Task。
- **执行中分别重启 Hermes 与 Codex，恢复后状态一致（§17.2 / §17.4）。**
- **杀掉 Codex 进程不放回，租约过期后 Janitor 按策略 requeue/fail（§17.5）。**

---

# 22. 开发规范

## 22.1 所有 Task 必须有 ID

格式：

```text
T-YYYYMMDD-XXXX
```

**生成规则（v2 明确，并发安全）**：

- Phase 3 起，ID 由 `task_manager` 通过 SQLite `counters` 表在**单事务**内生成：

```sql
INSERT INTO counters (name, value) VALUES ('task:20260817', 1)
ON CONFLICT(name) DO UPDATE SET value = value + 1
RETURNING value;
```

- Phase 1–2（DB 未接入）使用临时格式 `T-<ULID>`，进入 Phase 3 后不迁移历史临时 ID（仅测试数据）。
- 禁止用"读最大值再 +1"的方式生成 ID（并发下必然冲突）。

## 22.2 所有 Event 必须有 trace_id

Task 链共享一个 root trace。

## 22.3 SQLite 写入规则（v2：单一写者原则）

核心表（tasks / agents / task_runs / artifacts）的写入职责：

| 写者 | 允许写的内容 |
|---|---|
| **State Writer**（唯一事件写库者） | 由 Worker / Adapter 事件驱动的状态迁移（started / progress / completed / failed / blocked）、agents.last_seen_at / lease、artifacts 登记 |
| **Hermes**（task_manager 直写） | 仅限任务生命周期命令：create（含 ID 生成）、assign、cancel、retry 决策、review 结论 |
| **Janitor** | 仅限 §17.5 列出的对账修正，且每次修正必须同时发布 `system.audit` 事件 |

约束：

- Worker / Adapter **一律禁止**直接写 SQLite。
- State Writer 应用事件前必须按 §5.3 迁移表校验合法性，并以 `event_id` 去重；非法迁移拒绝 + `system.audit`。
- 所有 UPDATE 使用条件更新（`UPDATE ... WHERE status IN (...)`）做乐观并发控制，避免迟到事件覆盖新状态。

## 22.4 Artifact 必须可验证

保存：

```text
path
sha256
creator
created_at
task_id
```

## 22.5 幂等

所有任务执行接口必须支持：

```text
idempotency_key = task_id + ":" + attempt
```

- 写入 task.idempotency_key（UNIQUE 约束）。
- Adapter 收到与正在执行或已完成任务相同 key 的请求时，直接返回已有结果，不重复执行。
- 防止消息重放 / Hermes 重试导致重复执行。

---

# 23. 不建议的实现方式

不要：

### 1. GUI 自动点击作为主通信方式

```text
Hermes → AppleScript → ChatGPT App UI
```

可作为临时 Adapter，但不作为长期核心协议。

### 2. Agent 互相直接维护连接表

```text
Hermes knows Codex
Codex knows Kimi
Kimi knows Ops
...
```

会形成 N×N 连接关系。

### 3. 把 NATS 当数据库

Event 和 Current State 必须分离。JetStream 保留期有限，SQLite 才是事实源。

### 4. 每个 Agent 一套长期记忆

容易发生冲突和上下文污染。长期记忆只有一个逻辑实例，且只有 Hermes 可写。

### 5. 一开始引入 Kubernetes

当前规模没有必要。

### 6. 一开始开发 Web 管理平台

先把协作链打通。

---

# 24. 推荐本机进程布局

```text
launchd
├── ai.nats-server
├── ai.codex-adapter
├── ai.kimi-adapter          # later
├── ai.state-writer
├── ai.janitor               # Phase 4 起
├── ai.hindsight             # Memory 后端容器/进程
├── ai.agentgateway          # Phase 5
└── ai.hermes-integration
```

监听建议：

```text
NATS             127.0.0.1:4222
NATS Monitor     127.0.0.1:8222
Hermes A2A       127.0.0.1:8100
Codex Adapter    127.0.0.1:8201
Kimi Adapter     127.0.0.1:8202
Hindsight        127.0.0.1:8888
agentgateway     127.0.0.1:8300
```

**所有端口第一阶段只允许绑定 127.0.0.1**（见 §3.4 风险声明）。具体端口可调整。

---

# 25. 推荐仓库结构

```text
agentHub/
├── README.md
├── pyproject.toml
├── config/
│   ├── agents.yaml
│   ├── permissions.yaml
│   └── nats.yaml
├── docs/
│   └── adr/                      # ADR-0001 起，所有架构决策
│       ├── 0001-hermes-integration-language.md
│       └── 0002-memory-backend.md
├── src/
│   ├── common/
│   │   ├── models.py
│   │   ├── events.py
│   │   ├── memory.py             # MemoryService 接口定义
│   │   └── ids.py
│   ├── orchestrator/
│   │   ├── registry.py
│   │   ├── task_manager.py
│   │   ├── a2a_client.py
│   │   ├── nats_client.py
│   │   ├── recovery.py           # 启动恢复，§17.2
│   │   └── policy.py
│   ├── adapters/
│   │   ├── fake/                 # Phase 1 Fake Worker
│   │   ├── codex/
│   │   │   ├── server.py
│   │   │   ├── runner.py
│   │   │   └── card.py
│   │   └── kimi/
│   ├── state/
│   │   ├── db.py
│   │   ├── migrations/
│   │   ├── writer.py             # State Writer
│   │   └── janitor.py            # 对账，§17.5
│   ├── memory/
│   │   └── hindsight_client.py   # MemoryService 的 Hindsight 实现
│   └── cli/
│       └── agentctl.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── contract/                 # A2A Adapter 契约测试（见下）
├── scripts/
│   ├── bootstrap.sh
│   ├── start-dev.sh
│   └── healthcheck.sh
└── deploy/
    └── launchd/
```

**契约测试（v2 新增）**：`tests/contract/` 定义一套与具体 Agent 无关的 Adapter 行为规范（Agent Card 结构、A2A 状态映射、幂等键行为、队列行为、Artifact 完整性）。**同一套测试必须既能跑 Fake Worker，也能跑真实 Codex / Kimi Adapter**——这是"可替换 Worker"原则的强制保障，新 Adapter 接入的先决条件是通过全部契约测试。

---

# 26. Hermes 与 Codex 的实施分工

建议 Hermes 负责：

```text
architecture decisions
integration planning
task schema
routing rules
agent registry
Hermes integration
acceptance testing
ADR 撰写与维护
```

建议 Codex 负责：

```text
Python scaffolding
FastAPI adapter
NATS client
SQLite schema/migrations
State Writer / Janitor
CLI
unit tests
contract tests
integration tests
launchd plist
```

推荐协作模式：

```text
Hermes:
  设计一个可验收的小任务

Codex:
  实现 + 测试 + 输出 Artifact

Hermes:
  Review（completed → reviewed → accepted / rejected）

Codex:
  修复

Hermes:
  合并 / 接受
```

---

# 27. 第一轮给 Codex 的实施任务

建议第一轮不要让 Codex 一次性实现全部系统。

### Task 1

```text
Initialize repository structure and Python project.
```

验收：

- pyproject.toml
- basic package
- pytest works
- docs/adr/ 目录就绪

### Task 2

```text
Implement SQLite state store and migrations.
```

验收：

- agents/tasks/artifacts/task_runs/counters
- WAL
- 并发安全 ID 生成（§22.1）
- tests

### Task 3

```text
Implement NATS connection and event envelope.
```

验收：

- publish
- durable subscribe
- reconnect
- event_id 去重
- tests

### Task 4

```text
Implement fake A2A worker.
```

不要马上连真实 Codex。

Fake worker：

```text
receive task
sleep 1s
create artifact
publish completed
return result
```

### Task 5

```text
Connect Hermes test client to fake worker.
```

### Task 6

```text
Replace fake worker runtime with local Codex invocation.
```

### Task 7（v2 新增）

```text
Implement contract test suite; run against fake worker and Codex adapter.
```

这种顺序最容易定位问题。

---

# 28. MVP 完成标准

满足以下条件即可认为 MVP 完成（2026-08-17 盘点，证据见括号）：

- [x] Hermes 能看到至少两个 Agent。（registry + codex/kimi/fake 三个 Adapter，test_orchestrator_flow）
- [x] Hermes 可以创建 Task（ID 并发安全）。（counters 表原子自增，test_db 并发用例）
- [x] Hermes 可以通过 A2A 委派 Codex。（a2a_client + task_manager.delegate_task，Phase 2 验收）
- [x] Codex 可以执行真实开发任务。（test_codex_adapter，cliproxy/deepseek-v4-flash 真实通过）
- [x] Codex 可以返回 Artifact。（save_artifact 含 sha256 + workspace 文件收集）
- [x] NATS 可看到任务状态事件。（EventPublisher + JetStream AGENT_EVENTS，test_nats_events）
- [x] JetStream 可恢复未消费事件。（event_consumer + events-pending.jsonl spool 重发）
- [x] SQLite 可查询当前 Task 状态，且是唯一事实源。（StateWriter 唯一写者，agentctl 查询）
- [x] Hermes 重启后仍可恢复未完成任务（§17.2）。（recovery.py + 单测）
- [x] Codex 重启后不导致 Task 永久丢失。（状态在 SQLite；retry_task 重新入队）
- [x] Worker 失联后租约过期，Janitor 按策略处理（§17.4 / §17.5）。（janitor.py + 心跳租约，Phase 4）
- [x] 非法状态迁移被拒绝并留 audit 记录（§5.3 / §22.3）。（is_legal_transition 强制校验，test_state_store）
- [x] 重复事件 / 重复请求不产生重复执行（§22.5）。（A2aTaskStore 幂等键去重）
- [x] Artifact 可以追溯到 Task / Agent。（artifacts 表 task_id + sha256 + path）
- [x] 危险操作存在审批机制（blocked → input-required → 用户决策）。（approve_task/reject_task + agentctl task approve/reject）
- [x] 不需要人工复制粘贴 Agent 输出。（test_full_collaboration：kimi→codex depends_on 全自动链，§21 场景通过）
- [x] 契约测试同时对 Fake Worker 与 Codex Adapter 通过。（test_fake_worker_e2e 常驻；test_codex_adapter 门控通过）

---

# 29. 后续扩展方向

MVP 后再考虑：

```text
PostgreSQL
OpenTelemetry
Web UI
multi-host agents
NAS / VPS workers
Kubernetes
kagent
advanced scheduler
cost accounting
dynamic Agent Card discovery
semantic capability routing
artifact index
project knowledge graph（届时可评估 cognee / Graphiti 接入 Memory 接口）
```

当 Agent 数量达到约 10+，或开始跨 Mac / NAS / VPS / K8s 多机运行时，再认真评估 kagent 一类完整平台。

---

# 30. 关键设计结论

最终设计保持以下边界：

```text
Hermes
= Brain / Planner / Orchestrator / Supervisor / 唯一长期记忆写方

A2A
= Agent-to-Agent communication（状态映射见 §5.4）

agentgateway
= Communication governance / security / routing（Phase 5）

NATS JetStream
= Event bus / durable message backbone（传输层，非事实源）

SQLite
= Current system state（唯一事实源，写入受 §22.3 约束）

MCP
= Tool invocation

Workspace + Git
= Shared artifacts / project state

Memory 接口 + Hindsight
= Long-term memory（接口抽象，可替换，§15.3 / 附录 B）
```

第一阶段不要开发完整 Agent Hub。

真正需要自研的核心只有：

```text
Hermes orchestration integration（含 recovery）
+
Agent adapters（含 fake worker）
+
State writer + Janitor
+
small CLI
+
Memory 接口薄封装
```

整体应坚持"小内核、标准协议、可替换 Worker、事件驱动、状态独立、单一事实源"的原则。

---

# 附录 A：任务生命周期端到端时序（v2 新增）

```text
User      Hermes          TaskMgr/SQLite   StateWriter    NATS/JS      CodexAdapter    Codex   Janitor
 │          │                  │               │             │             │            │        │
 │ 目标     │                  │               │             │             │            │        │
 │────────►│                  │               │             │             │            │        │
 │          │ create_task      │               │             │             │            │        │
 │          │────────────────►│ INSERT queued │             │             │            │        │
 │          │                  │ (counters ID) │             │             │            │        │
 │          │ publish task.created            │             │             │            │        │
 │          │─────────────────────────────────────────────►│             │            │        │
 │          │ 路由(规则→技能→容量)│             │             │             │            │        │
 │          │ assign → UPDATE assigned        │             │             │            │        │
 │          │────────────────►│               │             │             │            │        │
 │          │ A2A SendMessage(idempotency_key)│             │             │            │        │
 │          │──────────────────────────────────────────────────────────►│            │        │
 │          │                  │               │             │  task.started            │        │
 │          │                  │               │             │◄────────────┤            │        │
 │          │                  │               │ consume+校验迁移(§5.3)      │            │        │
 │          │                  │ UPDATE working│◄────────────┤             │            │        │
 │          │                  │               │             │             │ 执行       │        │
 │          │                  │               │             │             │──────────►│        │
 │          │                  │               │             │ task.completed+artifacts │        │
 │          │                  │               │             │◄────────────┤            │        │
 │          │                  │ UPDATE completed            │             │            │        │
 │          │                  │◄──────────────┤             │             │            │        │
 │          │ A2A 取回 Artifact│               │             │             │            │        │
 │          │◄───────────────────────────────────────────────────────────┤            │        │
 │          │ review → reviewed → accepted/rejected(返工)    │             │            │        │
 │          │────────────────►│               │             │             │            │        │
 │ 最终汇报  │                  │               │             │             │            │        │
 │◄─────────┤                  │               │             │             │            │        │
 │          │                  │               │             │   每 30s heartbeat       │        │
 │          │                  │               │ UPDATE lease│◄────────────┤            │        │
 │          │                  │               │             │             │            │  60s   │
 │          │                  │ 对账修正+audit│◄─────────────────────────────────────────────────│
```

异常分支（省略图示，见 §17）：超时 → failed → retry_pending → queued；取消 → cancelled 级联；失联 → 租约过期 → Janitor 处置；Hermes 重启 → §17.2 恢复流程。

---

# 附录 B：长期记忆选型评估（2026-08）

候选对象均为开源自托管方案；评估约束：单台 Mac、少运维组件、Hermes 单写者、无多租户需求。

| 方案 | 许可 | 部署形状 | 模型 | 适配度评估 |
|---|---|---|---|---|
| **Hindsight**（Vectorize） | MIT | 单容器 + 内嵌 PostgreSQL，无外部依赖；有 MCP 路径 | retain/recall/reflect，多策略检索（向量+BM25+图+时序），自动归纳 observations | ✅ **默认选择**。部署最轻，接口与 §15.3 抽象一一对应，recall 无额外 LLM 成本。缺点：项目新、无 RBAC（本系统可接受） |
| Mem0 | Apache-2.0 | 内嵌存储起步；生产建议外部向量库 | 事实抽取 + 语义检索 | 🟡 第一备选。生态最大、SDK 最全；缺时序推理，生产自托管多一个向量库组件。Hindsight 出问题时切换它 |
| Graphiti（Zep 引擎） | Apache-2.0 | 需 Neo4j / FalkorDB / Kuzu | 时序知识图谱，事实带有效期窗口 | 🟡 能力最强但最重。Zep 社区版已弃用，自托管只剩引擎。未来确需"什么时间点什么是真的"类查询时再引入 |
| Letta | Apache-2.0 | 完整 Agent Runtime + 服务器 | 分层记忆，Agent 自管理 | ❌ 排除。它是 Agent 运行时而非记忆库，与"Hermes 是唯一大脑"的架构冲突 |
| Cognee | Apache-2.0 | 内嵌 SQLite/LanceDB/Kuzu 起步 | 文档 → 知识图谱 ECL 管道 | 🟡 后置候选。更适合项目知识图谱（§29 已列为扩展方向），而非对话式长期记忆 |

**注意**：以上各家的公开基准分数（LongMemEval / LoCoMo / BEAM）均为厂商自报且互相矛盾，不可作为选型依据；本决策基于部署形状、接口匹配度与架构契合度，并在 Phase 0 用 Spike 2 实测验证。

---

# 31. 官方参考

- A2A Protocol: https://a2a-protocol.org/
- MCP: https://modelcontextprotocol.io/
- NATS: https://docs.nats.io/
- agentgateway: https://agentgateway.dev/
- Hindsight: https://hindsight.vectorize.io/
- Mem0: https://github.com/mem0ai/mem0
- Graphiti: https://github.com/getzep/graphiti
- kagent: https://kagent.dev/
- Google ADK: https://google.github.io/adk-docs/
- Microsoft Agent Framework: https://learn.microsoft.com/en-us/agent-framework/

---

## 给 Hermes / Codex 的执行要求

实施时遵守：

1. 不一次性实现全部 Phase。
2. 每个 Phase 独立可运行、可测试、可回滚。
3. 先 Fake Worker，再接真实 Codex。
4. 先直连 A2A，再加入 agentgateway。
5. 先 CLI，后 Web UI。
6. 所有状态变化必须可追踪，且只允许 §5.3 表中的迁移。
7. 所有外部副作用必须有权限边界。
8. 每个实现任务必须提供测试和验收结果。
9. 发现设计不适合现有 Hermes/Codex 实际接口时，可以调整实现细节，但不得破坏本文定义的组件职责边界。
10. 任何架构性变更先记录 ADR（`docs/adr/`），再实施。
11. Worker / Adapter 永不直接写 SQLite（§22.3）。
12. 新 Adapter 接入前必须通过 `tests/contract/` 全部契约测试。
