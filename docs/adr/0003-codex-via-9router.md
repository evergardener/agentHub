# ADR-0003: Codex 模型提供方 = 9router（beta 测试密钥）

- 状态：accepted（2026-08-17 验收通过）
- 日期：2026-08-17
- 关联：Phase 2 Codex Adapter 验收；ADR-0002

## 背景

Codex CLI 默认走 OpenAI 账户，验收时该账户报 `workspace out of credits`。用户指示改用本机 9router 作为模型网关，并指定使用**测试密钥**而非生产密钥。

## 决策

1. `~/.codex/config.toml` 增加自定义 provider：
   ```toml
   model = "teamrouter/gpt-5.4-mini"
   model_provider = "9router"

   [model_providers.9router]
   name = "9router"
   base_url = "https://9router.evergardenviolet.top/v1"
   env_key = "NINEROUTER_API_KEY"
   wire_api = "responses"   # codex 0.145 已移除 "chat"
   ```
   原配置备份于 `~/.codex/config.toml.bak.<timestamp>`。
2. 密钥策略（用户指定）：使用 9router 中的 **`beta`（测试）key**，**不使用生产 key**。key 存于 macOS Keychain `keychain://agent-system/9router-api-key`；Codex Adapter 的 runner 在环境变量缺失时自动从 Keychain 注入。
3. 模型选择过程（实测记录）：
   - `teamrouter/deepseek-v4-flash-free`：上游免费额度耗尽（402）
   - `teamrouter/deepseek-v4-flash` 等付费线路：beta key 无余额（402）
   - `ds/*` 直连线路：不支持 Responses API（404）
   - **`teamrouter/gpt-5.4-mini`：Responses 与 Chat 双端点均可用** ✅
4. Hindsight 的 LLM 同步切换：`HINDSIGHT_API_LLM_API_KEY` 换为 beta key、`HINDSIGHT_API_LLM_MODEL` 换为 `teamrouter/gpt-5.4-mini`（`ds/*` 线路对 beta key 返回 NotFound）。retain 实测恢复。

## 已知限制

- 免费/付费 deepseek 线路余额恢复后可随时改回，只需编辑 `~/.codex/config.toml` 的 `model` 一行与 hindsight `.env`。
- `gpt-5.4-mini` 能力低于旗舰模型；复杂开发任务若质量不足，建议改 `teamrouter/gpt-5.4` 或恢复生产线路（需用户另行授权）。
- codex exec 沙箱内无法联网安装依赖（如 pytest 需预装），任务描述应避免假设可联网。

## 验证记录（2026-08-17）

- 手动 `codex exec`：创建 `hello.py` + `test_hello.py` ✅
- `LAS_RUN_CODEX=1 pytest tests/integration/test_codex_adapter.py` ✅（276s）
- Hindsight retain ✅（实体抽取正常）
