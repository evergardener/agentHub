# agentgateway 运行手册（Phase 5）

设计依据：§3.4 / Phase 5。gateway 只做通信治理，不做编排。

## 拓扑

```text
Hermes ──► 127.0.0.1:8300 (agentgateway, API key 认证)
              ├─ /agents/codex/* ──► 127.0.0.1:8201 (codex adapter)
              └─ /agents/kimi/*  ──► 127.0.0.1:8202 (kimi adapter)
```

所有端口 loopback only。Worker 不直接对外暴露；生产部署时 Hermes 只配
gateway 地址（`AGENT_GATEWAY_URL`）。

## 文件

- `infra/agentgateway/bin/agentgateway` — v1.4.1 darwin-arm64
  （sha256 已对照 GitHub release 校验，未入 git，.gitignore 排除）
- `infra/agentgateway/config.yaml` — 路由 / auth / ACL / timeout / retry

## 启动

```bash
GATEWAY_API_KEY=$(security find-generic-password -s agent-system -a gateway-api-key -w) \
  infra/agentgateway/bin/agentgateway -f infra/agentgateway/config.yaml
```

key 存于 Keychain `agent-system/gateway-api-key`，经环境变量注入，
配置文件里只写 `"$GATEWAY_API_KEY"`，不落明文。

## 治理行为

- **认证**：gateway 级 `apiKey` strict 模式；客户端用
  `Authorization: Bearer <key>`（`x-api-key` 不接受）。
- **ACL**：路由级 `authorization` 规则校验 key 元数据 `agents` 列表。
  禁用某 Agent = 从 `agents` 中移除其名字；agentgateway 热加载，
  无需重启。被拒请求返回 403。
- **超时/重试**：路由级 `timeout.requestTimeout`（codex 900s / kimi 300s）
  与 `retry`（2 attempts，仅 502/503，backoff 500ms）。

## Hermes 侧接线

`A2aClient.for_agent(name, direct_endpoint)`：
`AGENT_GATEWAY_URL` 非空时走 gateway（自动拼 `/agents/<name>` 前缀并注入
Bearer key），否则保持 Phase 1-4 的直连行为。task_manager 与 recovery
均已切换到该工厂方法。

## 验收

```bash
LAS_RUN_GW=1 .venv/bin/python -m pytest tests/integration/test_agentgateway.py
```

覆盖：无 key 401 / 经 gateway 委派成功 / 禁用 Agent 后 403 / 直连回退。

## 已知限制

- TLS/mTLS 未启用：全 loopback，第一阶段可接受；跨机部署前必须加。
- tracing（OTel）未接：§29 后置项。
- ACL 粒度是"key → agent 列表"，没有 per-task 策略；需要时引入
  JWT + CEL 细化。
