# local-agent-system

Hermes 主控的本地多 Agent 协作系统。设计依据：[`docs/Hermes_MultiAgent_Collaboration_Design_v2.md`](docs/Hermes_MultiAgent_Collaboration_Design_v2.md)。

## 组件边界（速览）

| 组件 | 职责 |
|---|---|
| Hermes | Brain / Planner / Orchestrator，唯一长期记忆写方 |
| A2A | Agent 间业务语义通信 |
| NATS + JetStream | 事件总线 / 可靠消息（非事实源） |
| SQLite（WAL） | 系统当前状态，**唯一事实源** |
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
Worker agent（codex / kimi / pi ...）**不打包进镜像**——宿主机自装后经心跳注册，
没注册就不可用（`agentctl agent list` 查看在线状态）。

```bash
cp .env.example .env     # 填 LAS_LLM_API_KEY / LAS_GATEWAY_API_KEY
docker compose up -d     # nats + state-writer + janitor + agentgateway
docker compose run --rm agentctl chat   # 与 hermes 对话
```

宿主机 worker 接入容器栈：

```bash
export LAS_NATS_URL=nats://127.0.0.1:4222
export LAS_AGENT_ENDPOINT=http://host.docker.internal:<port>
PYTHONPATH=src python -m adapters.<name>.server   # 或对应启动方式
```

PostgreSQL 状态库（M3，默认不启用）：`docker compose --profile postgres up -d`，
以 `LAS_DATABASE_URL` 切换后端。

## 规范要点

- Task 状态迁移只允许设计文档 §5.3 表中的迁移。
- Worker / Adapter 永不直接写 SQLite（§22.3 单一写者原则）。
- 架构变更先写 ADR（`docs/adr/`）再实施。
- 新 Adapter 必须通过 `tests/contract/` 全部契约测试。
