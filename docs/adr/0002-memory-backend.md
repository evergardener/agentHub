# ADR-0002: 长期记忆后端 = Hindsight（复用本机现有实例）

- 状态：accepted（2026-08-17 端到端验证通过）
- 日期：2026-08-17
- 关联：设计文档 v2 §15.3 / 附录 B；Phase 0 Spike 2

## 背景

设计文档选定 Hindsight 作为长期记忆默认实现，置于 `MemoryService` 接口之后。Spike 2 要求本地起容器跑通 retain/recall 最小用例。

## Spike 结果（实测）

1. **本机已运行 Hindsight 0.8.3**（docker compose：`hindsight-api` + `hindsight-worker` + `hindsight-control-plane`），无需新建容器：
   - API: `http://127.0.0.1:18888`（容器端口 8888 映射到宿主机 18888；宿主 8888 被 AllinSSL 占用）
   - 控制面: `http://192.168.7.10:19999`
   - `GET /health` → `{"status":"healthy","database":"connected"}` ✅
2. **该实例启用了 API Key 鉴权**（`HINDSIGHT_API_TENANT_API_KEY`）。2026-08-17 按用户指示轮换为随机生成的新 key：已更新 compose `.env`（旧值备份于 `.config-backups/`）、存入 macOS Keychain（`keychain://agent-system/hindsight-api-key`），客户端经 `HINDSIGHT_API_KEY` 环境变量注入。
3. 0.8.3 API 路由已勘察（openapi.json），客户端按此实现：
   - retain: `POST /v1/default/banks/{bank_id}/memories`
   - recall: `POST /v1/default/banks/{bank_id}/memories/recall`
   - reflect: `POST /v1/default/banks/{bank_id}/reflect`
4. 镜像 `ghcr.io/vectorize-io/hindsight:latest` 已拉取备用（如需独立实例）。
5. 附带发现：官方有 Codex 集成（`hindsight-integrations/codex`，hooks 机制）；本机另有一套自研 `agent-memory-*` 服务（127.0.0.1:7810）。两者均不改变本决策，记录备查。
6. **LLM 配置修复（2026-08-17，后更新）**：原配置模型 `ocg/qwen3.7-plus` 的路由（9router → opencode-go provider）无有效凭据，导致 retain 的事实抽取 100% 失败（实例健康检查只查数据库，故长期未暴露）。当日下午 9router 侧生产 key 被停用后，按用户指示统一改用 **beta 测试 key + `teamrouter/gpt-5.4-mini`**（详见 ADR-0003），retain/recall 端到端验证通过。如需更换模型，改 compose 目录 `.env` 后 `docker compose up -d` 即可。

## 决策

- 长期记忆后端 = **复用本机现有 Hindsight 0.8.3 实例**（`http://127.0.0.1:18888`），不新建容器。
- 实现 `src/memory/hindsight_client.py`（`MemoryService` 接口，stdlib urllib，零新增依赖）。
- scope → bank 映射：`user→las-user`、`project:<id>→las-project-<id>`、`system→las-system`。
- 密钥经环境变量 `HINDSIGHT_API_KEY` 注入（Keychain：`security find-generic-password -s agent-system -a hindsight-api-key -w`）。
- 注意：设计文档 v2 §24 中 "Hindsight 127.0.0.1:8888" 以本 ADR 的 **18888** 为准。

## 验证记录（2026-08-17）

- retain/recall 端到端通过（bank `agent-system-spike` 与 `las-project-local-agent-system`，含实体抽取、metadata 透传、scope→bank 映射）。
- 项目客户端 `src/memory/hindsight_client.py` 实测通过。

## 后果

- 优点：零新增运维组件；实例已稳定运行 3 周；接口抽象保留替换能力（Mem0 为第一备选）。
- 风险：与其他用途共享同一实例——通过 `las-` bank 前缀隔离；该实例的升级由用户统一管理。
- 待办：用户提供 API Key 后执行端到端 retain/recall 验证（记入 Phase 7 memory MCP 封装的前置条件）。
