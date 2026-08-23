# agentHub

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

多 Agent/多步骤委派使用版本化结构化 Task Plan：每步绑定 Agent/Profile
version、依赖、预期操作/产物和验收条件；用户介入或 Profile 漂移会使旧计划
fail-closed，任务详情可追溯完整计划链。

## 开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Docker 部署（Evolution v3 M2）

控制面一体化镜像（hermes-brain / state-writer / janitor / notifier /
agentgateway / agentctl）。
Worker agent（codex / kimi / dsh / pi ...）**不打包进镜像**——宿主机自装后经心跳注册，
没注册就不可用（`agentctl agent list` 合并静态 catalog 与心跳租约，显示
`disabled/static/online/offline`）。

```bash
cp .env.example .env && chmod 600 .env
# 填入 .env.example 标注的生产密钥后，先做不泄露密钥值的预检
python3 scripts/production-preflight.py .env
docker compose up -d     # 另含 notifier / orchestrator / webui / jaeger
docker compose run --rm agentctl chat   # 与 hermes 对话
```

仓库/目录名称使用 `agentHub`，但 Compose 项目身份暂时固定为
`local-agent-system`，用于在目录改名后继续复用既有容器、网络和持久卷。不要把
基础设施资源改名与仓库目录改名一起执行；资源改名必须另行备份、迁移和验证。

- Web UI（看板 / 告警 / 任务详情 / 事件流 / 审批中心）：http://127.0.0.1:18070
- Jaeger（OTel trace 查询）：http://127.0.0.1:16686
  （`LAS_OTEL_ENDPOINT` 置空即关闭 tracing，默认 NoOp 零开销）
- Compose 生产模式对 WebUI 与 Orchestrator 认证 fail-closed；缺失或弱密钥时
  相应服务拒绝启动。使用 `scripts/production-preflight.py` 在启动前一次检查。
- DSH rc.7 尚无可验证的原生权限强制；当前构建默认拒绝其模型 prompt、readiness
  返回不可用并阻断生产路由。DSH 自身 Web UI 的独立使用不受影响。
- 一致性数据保护使用 `scripts/control-plane-backup.py create|verify`；备份不含
  `.env` 密钥，详见部署文档。
- 告警默认持久化到 WebUI；配置 `LAS_ALERT_WEBHOOK_URL` 后由 notifier 经
  HTTPS 投递，失败自动退避并升级，不会因外部通知不可用而丢告警。
- gateway 默认仅供同机 loopback 使用；跨主机部署使用独立的 TLS 1.3/mTLS +
  strict JWT 剖面 `docker-compose.gateway-remote.yml`，详见
  [`docs/agentgateway.md`](docs/agentgateway.md)，不要直接暴露默认 8300 端口。

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
  DSH 命令仅在无 shell 组合/扩展且 operation 与目标路径可规范化到任务工作区时
  才可逐次放行；事件及 history Artifact 会有界保存并脱敏。持久权限仍固定
  `read-only`，修改仅使用绑定原 RPC 的 `allowed-once`。

PostgreSQL 状态库（M3，compose 默认启用）：控制面默认使用
`LAS_DATABASE_URL=postgresql://agenthub:***@postgres:5432/agenthub`；
回退 SQLite 在 `.env` 设 `LAS_DATABASE_URL=sqlite:////data/workspace/runtime/agent-state.db`，
外部 PG 直接改 URL 即可。

## 规范要点

- Task 状态迁移只允许设计文档 §5.3 表中的迁移。
- Worker / Adapter 永不直接写 SQLite（§22.3 单一写者原则）。
- 架构变更先写 ADR（`docs/adr/`）再实施。
- 新 Adapter 必须通过 `tests/contract/` 全部契约测试。
