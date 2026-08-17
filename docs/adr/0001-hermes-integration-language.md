# ADR-0001: Hermes 集成层语言与形态

- 状态：accepted（provisional，Phase 4 前复核）
- 日期：2026-08-17
- 关联：设计文档 v2 §19 前置 Spike 1

## 背景

设计文档要求 Hermes 侧增加"轻量集成层，而不是重写 Hermes"（§11），并预留了语言悬念：若 Hermes 插件机制更适合 TypeScript，则仅 Adapter 用 Python，集成层保持 Hermes 原生语言。

## Spike 结果

本次 Spike 环境中无法直接检查 Hermes 本体的插件机制（Hermes 不在本会话可观测范围内）。因此采用对 Hermes 内部机制**零假设**的方案。

## 决策

集成层实现为**独立 Python 3.12 包 + 进程**（`src/orchestrator/`），对 Hermes 暴露两类接入点：

1. **MCP 工具**（首选）：把 orchestrator 能力（create_task / delegate_task / wait_task / review_result / retry_task / cancel_task / list_agents）封装为 MCP server。MCP 是本系统各 Agent 的公共工具协议（设计文档 §3.7），无论 Hermes 是何种运行时都能接入。
2. **CLI**（兜底）：`agentctl` 子命令覆盖同等能力，Hermes 可通过受限 shell 调用。

Hermes 与 Worker 之间的 A2A 通信由集成层内的 `a2a_client.py` 承担。

## 后果

- 优点：不重写 Hermes；语言栈与 Adapter / State Writer 统一为 Python；CLI 天然支持人工运维。
- 代价：若 Hermes 原生插件机制为 TypeScript，MCP 接入层会产生一个额外进程（可接受，launchd 管理）。
- 复核点：Phase 4 开始时用 Hermes 实际验证 MCP 接入；若不可行，降级为 CLI 调用，并将本 ADR 标记 superseded。
