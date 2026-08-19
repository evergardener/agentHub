# ADR-0004: 持久协作会话与分层授权

- 状态：Accepted
- 日期：2026-08-19

## 背景

现有系统以一次性 Task 委派为中心，Hermes 消息只保存在进程内存，无法保证跨日期恢复同一
Agent Session，也不能安全支持子 Agent 协作和用户实时介入。关键词审批还会把未识别操作默认
视为只读。

## 决策

1. 在 Task 之上增加 Conversation 和 Collaboration；
2. 持久化 Message、Agent Session Binding、Context Revision 和 ActionIntent；
3. 消息先落 PostgreSQL 并取得单调 sequence 后投递；
4. 用户 > Hermes > 子 Agent，子 Agent 无审批权；
5. 子 Agent 协作必须经过 Registry 和 Policy；
6. 未识别操作 fail-closed；
7. 用户 steer/takeover 提升 revision，旧 revision 未执行写许可失效；
8. 原生 Session resume 优先，结构化 Context Snapshot 重建为兜底。

## 后果

- 能实现跨进程、跨日期连续协作和用户可介入会话；
- 数据模型与 Adapter 接口需要扩展；
- WebUI 可以从同一事实源重建完整协作时间线；
- legacy 一次性 Task 继续兼容，但新能力只建立在持久 Collaboration 上。
