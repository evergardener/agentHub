# local-agent-system

Hermes 主控的本地多 Agent 协作系统。现行产品与开发基线：

- [`docs/AgentHub_Product_Requirements_v1.md`](docs/AgentHub_Product_Requirements_v1.md)
- [`docs/AgentHub_Target_Architecture_v1.md`](docs/AgentHub_Target_Architecture_v1.md)
- [`docs/AgentHub_Implementation_Plan_v1.md`](docs/AgentHub_Implementation_Plan_v1.md)

历史完整设计依据：[`docs/Hermes_MultiAgent_Collaboration_Design_v2.md`](docs/Hermes_MultiAgent_Collaboration_Design_v2.md)。

## 组件边界（速览）

| 组件 | 职责 |
|---|---|
| Hermes | Brain / Planner / Orchestrator，唯一长期记忆写方 |
| A2A | Agent 间业务语义通信 |
| NATS + JetStream | 事件总线 / 可靠消息（非事实源） |
| PostgreSQL（默认）/ SQLite（回退） | 系统当前状态，**唯一事实源** |
| MCP | 工具调用层 |
| Workspace + Git | 共享工件 / 项目状态 |
| Memory 接口 + Hindsight | 长期记忆（可替换实现） |

## 开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Docker 部署（Evolution v3 M2）

控制面一体化镜像（hermes-brain / state-writer / janitor / agentgateway / agentctl）。
Worker agent（codex / kimi / dsh / pi ...）**不打包进镜像**——宿主机自装后经心跳注册，
没注册就不可用（`agentctl agent list` 查看在线状态）。

```bash
cp .env.example .env     # 填 LAS_LLM_API_KEY / LAS_GATEWAY_API_KEY
docker compose up -d     # nats + postgres + state-writer + janitor + agentgateway + webui + jaeger
docker compose run --rm agentctl chat   # 与 hermes 对话
```

- Web UI（看板 / 任务详情 / 事件流 / 审批中心）：http://127.0.0.1:18070
- Jaeger（OTel trace 查询）：http://127.0.0.1:16686
  （`LAS_OTEL_ENDPOINT` 置空即关闭 tracing，默认 NoOp 零开销）

宿主机 worker 接入容器栈：

```bash
# 手动启动（开发调试）
./scripts/agent-worker.sh codex

# DSH：先启动其独立 Web Runtime，再启动 AgentHub Adapter
dsh web --host 127.0.0.1 --port 3080
./scripts/agent-worker.sh dsh

# 开机/登录自启（launchd，崩溃自动重启）
./scripts/install-agent-autostart.sh codex           # 安装并启动
./scripts/install-agent-autostart.sh codex uninstall # 移除
```

- 监听地址：`LAS_ADAPTER_BIND`（默认 `127.0.0.1,192.168.7.10`，单地址不可用自动降级）
- 调用方鉴权：`LAS_ADAPTER_TOKEN` 非空时，除 `/health` 外一律要求 `X-Agent-Token` 头；
  hermes 直连/经 gateway 都会自动携带（gateway 透传）。生产/常驻部署必须配置。
- 日志：`~/Library/Logs/agenthub-<agent>.log`
- DSH Adapter 默认连接 `LAS_DSH_WEB_URL=http://127.0.0.1:3080`。DSH Web
  仍可独立使用；AgentHub 通过其原生持久 session API 续聊和恢复。新建 Hub
  session 默认执行 DSH `/permission read-only`，不会继承宽松的 Web 默认值。
  原生 approval/question 会实时进入 A2A `pendingInteractions`，经持久
  ActionIntent/用户或 Hermes 决策后由 `/api/respond` 回到同一 DSH turn。

PostgreSQL 状态库（M3，compose 默认启用）：控制面默认使用
`LAS_DATABASE_URL=postgresql://agenthub:***@postgres:5432/agenthub`；
回退 SQLite 在 `.env` 设 `LAS_DATABASE_URL=sqlite:////data/workspace/runtime/agent-state.db`，
外部 PG 直接改 URL 即可。

## 规范要点

- Task 状态迁移只允许设计文档 §5.3 表中的迁移。
- Worker / Adapter 永不直接写 SQLite（§22.3 单一写者原则）。
- 架构变更先写 ADR（`docs/adr/`）再实施。
- 新 Adapter 必须通过 `tests/contract/` 全部契约测试。
