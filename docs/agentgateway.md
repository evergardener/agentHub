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
- `infra/agentgateway/config.remote.yaml` — 跨主机 TLS 1.3/mTLS + strict JWT 剖面
- `docker-compose.gateway-remote.yml` — 独立跨主机 gateway 部署入口

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

## 跨主机剖面

默认 `config.yaml` / `config.docker.yaml` 仍只用于同机 loopback。跨主机时使用
独立 `config.remote.yaml`，由 agentgateway 原生终止 TLS，不在其前面叠加另一个
身份源。该剖面同时要求：

- TLS 1.3；服务端证书必须包含客户端使用的 DNS/IP SAN；
- mTLS，`root` 只信任签发 AgentHub 调用方证书的 client CA；
- strict JWT，固定 `iss`、`aud` 和本地只读 JWKS；
- JWT 自定义 claim `role=orchestrator`，`agents` 为逗号分隔 allowlist；
- 每条路由继续独立限流，并用 CEL 对目标 Agent 做 claim ACL。

一个最小 JWT payload 语义如下（只示意 claims，不是可用 token）：

```json
{
  "iss": "https://identity.example/",
  "aud": "agenthub-gateway",
  "sub": "hermes-primary",
  "exp": 1780000000,
  "nbf": 1779999700,
  "role": "orchestrator",
  "agents": "codex,kimi,dsh"
}
```

### 服务端部署

使用组织 PKI/OIDC 导出的真实材料，不在仓库生成或保存 CA、私钥、JWT。先准备
一个权限为 `0600` 的部署 env 文件；路径均为 gateway 主机上的绝对路径：

```dotenv
AGENTHUB_IMAGE=ghcr.io/evergardener/agenthub@sha256:<approved-digest>
LAS_GATEWAY_SERVER_CERT_FILE=/etc/agenthub/pki/gateway.pem
LAS_GATEWAY_SERVER_KEY_FILE=/etc/agenthub/pki/gateway-key.pem
LAS_GATEWAY_CLIENT_CA_FILE=/etc/agenthub/pki/hermes-client-ca.pem
LAS_GATEWAY_JWKS_FILE=/etc/agenthub/identity/gateway-jwks.json
LAS_GATEWAY_JWT_ISSUER=https://identity.example/
LAS_GATEWAY_JWT_AUDIENCE=agenthub-gateway
LAS_GATEWAY_BIND_ADDRESS=0.0.0.0
LAS_GATEWAY_REMOTE_PORT=8443
LAS_CODEX_BACKEND=host.docker.internal:8201
LAS_KIMI_BACKEND=host.docker.internal:8202
LAS_DSH_BACKEND=host.docker.internal:8203
```

用发布流水线验证过的 image digest 部署；首次启动前先渲染并检查最终配置：

```bash
docker compose --env-file /etc/agenthub/gateway.env \
  -f docker-compose.gateway-remote.yml config --quiet
docker compose --env-file /etc/agenthub/gateway.env \
  -f docker-compose.gateway-remote.yml up -d
```

只在防火墙放行 Hermes 来源网段到 8443；8201/8202/8203 仍不得对 Hermes 网段或
公网开放。JWKS 轮换采用“先同时发布新旧公钥 → 签发新 token → 受控重启 gateway
加载新 JWKS → 确认旧 token 失效后移除旧公钥”，证书轮换同样走受控滚动重启。

### Hermes 客户端

Hermes 侧 `.env` 不保存 JWT 内容，只保存文件路径：

```dotenv
LAS_GATEWAY_URL=https://gateway.example:8443
LAS_GATEWAY_API_KEY=
LAS_GATEWAY_JWT_FILE=/run/secrets/agenthub-gateway.jwt
LAS_GATEWAY_CA_FILE=/run/secrets/gateway-server-ca.pem
LAS_GATEWAY_CLIENT_CERT_FILE=/run/secrets/hermes-client.pem
LAS_GATEWAY_CLIENT_KEY_FILE=/run/secrets/hermes-client-key.pem
```

JWT 文件和 client key 必须仅 owner 可访问。Hermes 在每次 gateway HTTP 请求前
重新读取 JWT 文件，因此签发端可以写临时文件、`chmod 600` 后原子 rename 覆盖；
不得在日志或 WebUI 展示 token。生产预检会拒绝跨主机 HTTP、缺失 JWT/mTLS 文件
或权限过宽的私密文件：

```bash
python3 scripts/production-preflight.py --strict .env
```

上线验收必须至少证明：无客户端证书时 TLS 握手失败；缺失/过期/错误 audience
JWT 返回 401；claim 不含目标 Agent 返回 403；正确 mTLS + JWT 可完成 A2A 委派；
轮换后新凭据可用且旧凭据失效。完成真实目标环境演练前，不把远程剖面视为已投产。

## 已知限制

- 默认 gateway 与调用方仍在同一主机、只监听 loopback；跨机必须显式使用上述
  remote 剖面，禁止直接把默认 8300 映射改为非 loopback。
- tracing（OTel）未接：§29 后置项。
- loopback ACL 粒度仍是"key → agent 列表"，没有 per-task 策略；remote 剖面已
  使用 strict JWT claims + CEL。两种剖面刻意不叠加身份源。
