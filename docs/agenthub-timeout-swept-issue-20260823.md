# agentHub 任务被 janitor 误判超时失败 — 问题汇总

> 供手动转交 Codex 修复。工作目录：
> `/Users/evergarden/Data/current-documents/Projects/local-agent-system`
> 分支 `main`，HEAD `1f754fb`。

## 一、现象

- 任务 `T-20260823-0002`（Codex 实现「任务显式验收状态机」）在 **12:03:31** 被置为 `failed`。
- 生成一条 **critical** 告警：

  ```text
  告警 ID:     AL-9eefecb6ff594749
  kind:        timeout_swept
  severity:    critical
  source:      janitor
  task_id:     T-20260823-0002
  ```

## 二、根因

`src/state/janitor.py` 的 `_sweep_timeouts()`（第 84–102 行）对 `working` 任务按 **`started_at` 一次性计时**：

```python
rows = self.conn.execute(
    "SELECT id, started_at, timeout_seconds FROM tasks"
    " WHERE status = 'working' AND started_at IS NOT NULL;")
for r in rows:
    started = datetime.fromisoformat(r["started_at"])
    limit = r["timeout_seconds"] or 1800
    if (now - started).total_seconds() > limit:
        state_store.transition_task(..., TaskStatus.FAILED,
                                    error_message="timeout_swept")
```

证据（production DB）：

```text
T-20260823-0002:
  started_at       2026-08-23 11:24:42
  completed_at(失败) 2026-08-23 12:03:31
  timeout_seconds  1800
```

累计 38 分钟 > 30 分钟阈值，被扫掉。期间该任务 **多次进入 `blocked` 等待原生编辑/命令审批**（见 task_runs，共 8 个 run，均在审批后恢复 `working`）。

**缺陷点：审批等待期被计入执行超时。** janitor 用任务首次 `started_at` 做单点计时，每次 `blocked → working` 恢复时未重置或暂停计时，导致「人工逐次审批的真实交互任务」被误判为执行超时。

## 三、连带隐患

- 任务被置 `failed` 后，Codex 又发出第 8 个 edit 审批请求（`task.input_required`），被 state-writer 拒绝：

  ```text
  system.audit:
    rejected_event task.input_required
    reason: illegal transition or unknown task  (failed → blocked 非法迁移)
  ```

- 失败后无法 `failed → blocked` 直接续跑，正常收尾被打断。

## 四、修复建议（供 Codex 设计）

1. **重新定义超时计时基线**：不应从任务首次 `started_at` 计。建议改为「最近一次进入 `working` 的时间」或「距最近一次事件超过 timeout 无新事件」才判超时。
2. **`blocked` 等待审批期间不消耗超时预算**：`blocked` 状态下暂停计时，恢复 `working` 时重置 / 续算。
3. 明确 `timeout_swept` 只针对「持续 working、无事件推进」的真僵死任务；人工审批等待属于正常流程，不应触发 sweep。
4. 补充单测覆盖该迁移 / 计时行为，并用回归测试验证「多次 blocked→working 的真实任务不会被 timeout_swept」。

## 五、约束与上下文（Codex 需遵守）

- **工作目录**：`/Users/evergarden/Data/current-documents/Projects/local-agent-system`
- **当前分支**：`main`，HEAD `1f754fb`
- **改动范围**：只改 janitor 计时逻辑 + 单测；本次不触碰其他模块。
- **不部署、不 commit**：产出 diff 与测试结果即可（源码缺陷修复归 Codex，Hermes 负责验收）。
- 库为 PostgreSQL（`docker compose` 起 local-agent-system-postgres）；本地测试可先跑 `tests/unit`。
- `git status` 当前有未提交改动与未跟踪文件，属既有工作区状态，**不得覆盖、commit 或部署**，只在其上叠加本次修复。