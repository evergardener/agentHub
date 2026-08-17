# ADR-0002: 长期记忆后端 = Hindsight（复用本机现有实例）

- 状态：accepted
- 日期：2026-08-17
- 关联：设计文档 v2 §15.3 / 附录 B；Phase 0 Spike 2

## 背景

设计文档选定 Hindsight 作为长期记忆默认实现，置于 `MemoryService` 接口之后。Spike 2 要求本地起容器跑通 retain/recall 最小用例。

## Spike 结果（实测）

1. **本机已运行 Hindsight 0.8.3**（docker compose：`hindsight-api` + `hindsight-worker` + `hindsight-control-plane`），无需新建容器：
   - API: `http://127.0.0.1:18888`（容器端口 8888 映射到宿主机 18888；宿主 8888 被 AllinSSL 占用）
   - 控制面: `http://192.168.7.10:19999`
   - `GET /health` → `{"status":"healthy","database":"connected"}` ✅
2. **该实例启用了 API Key 鉴权**：未带 key 的 retain/recall 返回 `Authentication failed: Invalid API key`。密钥属用户凭据，本次 Spike 未读取；端到端 retain/recall 验证待用户提供 key 后执行（建议存入 `keychain://agent-system/hindsight-api-key`）。
3. 0.8.3 API 路由已勘察（openapi.json），客户端按此实现：
   - retain: `POST /v1/default/banks/{bank_id}/memories`
   - recall: `POST /v1/default/banks/{bank_id}/memories/recall`
   - reflect: `POST /v1/default/banks/{bank_id}/reflect`
4. 镜像 `ghcr.io/vectorize-io/hindsight:latest` 已拉取备用（如需独立实例）。
5. 附带发现：官方有 Codex 集成（`hindsight-integrations/codex`，hooks 机制）；本机另有一套自研 `agent-memory-*` 服务（127.0.0.1:7810）。两者均不改变本决策，记录备查。

## 决策

- 长期记忆后端 = **复用本机现有 Hindsight 0.8.3 实例**（`http://127.0.0.1:18888`），不新建容器。
- 实现 `src/memory/hindsight_client.py`（`MemoryService` 接口，stdlib urllib，零新增依赖）。
- scope → bank 映射：`user→las-user`、`project:<id>→las-project-<id>`、`system→las-system`。
- 密钥经环境变量 `HINDSIGHT_API_KEY` 注入（Keychain 引用，设计文档 §14）。
- 注意：设计文档 v2 §24 中 "Hindsight 127.0.0.1:8888" 以本 ADR 的 **18888** 为准。

## 后果

- 优点：零新增运维组件；实例已稳定运行 3 周；接口抽象保留替换能力（Mem0 为第一备选）。
- 风险：与其他用途共享同一实例——通过 `las-` bank 前缀隔离；该实例的升级由用户统一管理。
- 待办：用户提供 API Key 后执行端到端 retain/recall 验证（记入 Phase 7 memory MCP 封装的前置条件）。
