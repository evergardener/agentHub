# agentgateway 运行手册（Phase 5）

设计依据：§3.4 / Phase 5。gateway 只做通信治理，不做编排。

## 拓扑

```text
Hermes ──► 127.0.0.1:8300 (agentgateway, API key 认证)
              ├─ /agents/codex/* ──► 127.0.0.1:8201 (codex adapter)
              ├─ /agents/kimi/*  ──► 127.0.0.1:8202 (kimi adapter)
              └─ /agents/dsh/*   ──► 127.0.0.1:8203 (dsh adapter)
```

所有端口 loopback only。Worker 不直接对外暴露；生产部署时 Hermes 只配
gateway 地址（`AGENT_GATEWAY_URL`）。

## 文件

- `infra/agentgateway/bin/agentgateway` — v1.4.1 darwin-arm64
  （sha256 已对照 GitHub release 校验，未入 git，.gitignore 排除）
- `infra/agentgateway/config.yaml` — 路由 / auth / ACL / 限流 / timeout / retry

## 启动

```bash
export LAS_GATEWAY_API_KEY=<gateway-key>   # 或旧名 GATEWAY_API_KEY
infra/agentgateway/bin/agentgateway -f infra/agentgateway/config.yaml
```

key 只经环境变量注入（M2 起 env-only，不再使用 macOS Keychain），
配置文件里只写 `"$GATEWAY_API_KEY"`，不落明文。

## 治理行为

- **认证**：gateway 级 `apiKey` strict 模式；客户端用
  `Authorization: Bearer <key>`（`x-api-key` 不接受）。
- **ACL**：路由级 `authorization` 规则校验 key 元数据 `agents` 列表。
  禁用某 Agent = 从 `agents` 中移除其名字；agentgateway 热加载，
  无需重启。被拒请求返回 403。
- **限流**：每条 Agent 路由有独立的本地 token bucket，初始最多突发 30 次，
  每 60 秒补充 30 次。超过额度返回 429；不同 Agent 的额度互不影响。
- **超时/重试**：路由级 `timeout.requestTimeout`（codex/dsh 900s / kimi 300s）
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

覆盖：配置 schema / 无 key 401 / 经 gateway 委派成功 / 禁用 Agent 后 403 /
路由限流 429 / 直连回退。

## 已知限制

- TLS/mTLS 未启用：当前 gateway 与调用方均在同一主机、只监听 loopback；
  跨机部署前必须切换到独立 HTTPS/mTLS 配置，禁止直接修改本剖面为非 loopback。
- tracing（OTel）未接：§29 后置项。
- ACL 粒度是"key → agent 列表"，没有 per-task 策略；需要时引入
  strict JWT（固定 issuer/audience/JWKS）+ CEL 细化。当前 loopback 剖面不叠加
  JWT，避免同时维护两套身份源却没有获得新的信任边界。
