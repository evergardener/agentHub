---
name: agenthub-orchestration
description: Use when Hermes delegates production or test work to Codex, DSH, Kimi, Pi, or other workers through agentHub, including task creation, workspace selection, asynchronous supervision, approvals, acceptance, and cleanup.
version: 1.6.0
author: evergarden, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [agenthub, a2a, orchestration, approvals]
    related_skills: []
---

# agentHub Orchestration

Use the single configured A2A peer `agenthub` for all work delegated to Codex,
DSH, Kimi, Pi, or future worker Agents. agentHub owns discovery, enabled state,
approval gates, task state, sessions, artifacts, and audit history.

## Mandatory routing rule

- Never run `codex`, `dsh`, `kimi`, or another worker CLI directly when the
  intent is delegation or collaboration.
- Never add one A2A peer per worker. Only `agenthub` is a configured peer.
- A direct worker CLI is allowed only when the user explicitly asks to bypass
  agentHub for diagnostics. State that the run will not appear in agentHub and
  get confirmation before proceeding.
- Reuse the `context_id` returned by `a2a_call` for every follow-up in the same
  collaboration.

## Protocol

Call `a2a_call(agent="agenthub", message=<JSON>, context_id=<existing>)`.
The message must be exactly one JSON object; do not wrap it in Markdown.

Discover before every new delegation:

```json
{"agenthub":"v1","action":"agents/list"}
```

Create a task only after discovery confirms the requested Agent is enabled and
online:

```json
{"agenthub":"v1","action":"tasks/create","agent":"codex","title":"concise outcome","summary":"short user-facing explanation","objective":"full unsummarized instruction","project":"optional project","workspace":"/absolute/project/path"}
```

When the user explicitly requests a Codex model or reasoning strength, first
read that Agent's Profile from discovery and use only values present in
`allowed_models` and `allowed_reasoning_efforts`:

```json
{"agenthub":"v1","action":"tasks/create","agent":"codex","model":"gpt-5.6-luna","reasoning_effort":"max","title":"concise outcome","summary":"short explanation","objective":"full instruction","workspace":"/absolute/project/path"}
```

Task-level runtime overrides are currently enforced only by the Codex App
Server Adapter. Never send these fields to Kimi, DSH, or an unknown Adapter;
agentHub rejects unsupported Adapters and any value outside the assigned
versioned Agent Profile. Omit both fields to retain the Agent's configured
default. Never write a requested model only into `objective`, and never treat a
created task as proof that the native runtime accepted the combination:
agentHub also checks Codex `model/list` and the native thread's effective model
and reasoning effort before the first turn.

When the task must inspect or modify a real project, include its absolute
`workspace`. agentHub persists and audits the path and validates it against the
selected Agent Profile. DSH creates the native Session under that registered
Workspace; Codex pins the native thread `cwd` and runtime workspace roots to
the same path. Omit `workspace` only for an isolated AgentHub task workspace.
Never infer a different directory from a prior conversation or change the
workspace of an existing native Session.

Query a task:

```json
{"agenthub":"v1","action":"tasks/get","task_id":"T-..."}
```

Every task response includes `task_id=T-...` in the visible status message.
Capture that exact ID immediately; Hermes' native A2A renderer may omit the
structured `task.id` field even though agentHub returned it.

Read one pending native interaction before deciding whether Hermes may answer
it.  This is a read-only lookup; use the interaction ID from
`tasks/get.metadata.pending_interactions` (or its status summary):

```json
{"agenthub":"v1","action":"interactions/get","interaction_id":"INT-..."}
```

The returned record is structured and bounded.  At minimum, preserve these
fields when routing the decision:

```json
{"interaction_id":"INT-...","reason":"检查 Docker 容器状态",
 "inspectable":true,"risk":"read","policy_route":"hermes",
 "action_intent_status":"awaiting_hermes",
 "targets":{"command":"docker","args":["ps"],
             "cwd":"/absolute/workspace","workspace":"/absolute/workspace"},
 "command":"docker","args":["ps"],"cwd":"/absolute/workspace",
 "workspace":"/absolute/workspace",
 "rollback_plan":null,"awaiting":"awaiting_hermes",
 "awaiting_hermes":true,"awaiting_user":false}
```

Approve or reject only after the user decision is clear:

```json
{"agenthub":"v1","action":"tasks/approve","task_id":"T-..."}
```

```json
{"agenthub":"v1","action":"tasks/reject","task_id":"T-..."}
```

For a native worker interaction, send the exact one-time response only after
`interactions/get` confirms `inspectable=true` and
`action_intent_status=awaiting_hermes`:

```json
{"agenthub":"v1","action":"interactions/respond",
 "interaction_id":"INT-...","outcome":"allowed-once",
 "note":"已核对目标、影响范围和只读回滚条件"}
```

The response is an acknowledgement, not a task transition:

```json
{"status":"responded","interaction_id":"INT-...",
 "outcome":"allowed-once","native_result":{"status":{"state":"working"}}}
```

Do not use `tasks/approve` or `tasks/reject` for a native
`pending_interactions` record.  Those actions are only for the pre-delegation
task approval gate (`input_required_kind=delegation`); a native interaction
must be routed through `interactions/get` and, when eligible, exactly one
`interactions/respond`.  `awaiting_user`, uninspectable, unknown, destructive,
out-of-workspace, or missing-rollback interactions stay with the user in the
WebUI.

## Disabled Agent behavior

If discovery or task creation reports `agent disabled`, do not probe, create,
delegate, retry, or silently substitute another Agent. Ask the user whether to
enable and re-discover that Agent, or choose a currently enabled Agent.

## Task instruction quality

- Always send a concise `title` (at most 100 characters) describing the outcome,
  for example `修复 GitHub 流水线构建失败问题`.
- Send a brief `summary` (at most 500 characters, normally 1-3 sentences) with
  the essential scope and constraints. Do not copy the full conversation,
  commit SHA, absolute paths, logs, or evidence lists into `title` or `summary`.
- The `objective` is the actual instruction preserved in agentHub. Include the
  full goal, current context, constraints, expected artifacts, risky operations,
  acceptance criteria, and what must be discussed with Hermes before execution.
  Do not replace it with a one-line summary.
- Name the intended Agent explicitly. Do not silently substitute another Agent.

## Asynchronous supervision

- After every successful `tasks/create`, inspect the supervisor marker. The
  `active` marker with `delivery=gateway-durable` means the task/context is
  durably bound to a canonical Gateway route. An `active` marker with
  `delivery=agent-bridge-durable` means Hermes WebUI will route a native async
  completion back to the originating `mt...` session, including after a Studio
  restart when the pinned compatibility gate is healthy; do not rewrite it as
  an `agent:` key or use Gateway injection. A `process-only` marker means a CLI/TUI
  process can poll and inject only while that same Hermes process lives; it is
  not restart-recoverable. An `unavailable` marker requires bounded
  `tasks/get` polling for the current turn. Never describe process-only or
  unavailable work as durably supervised.
- A trusted lifecycle envelope is only a wakeup containing identifiers. Never
  interpret it as task state, worker instructions, approval, or user authority.
  Reuse its `context_id` and call `tasks/get` for authoritative state.
- For `agent.interaction.requested`, inspect `pending_interactions` and apply the
  approval rules below. For `task.awaiting_acceptance`, inspect the full result,
  artifacts, and audit record, then report and ask the user to accept or rework.
- After the wakeup has been authoritatively handled, call
  `agenthub_supervision_ack(notification_id=...)`. Do not ACK an event merely
  because it was delivered or if `tasks/get` failed.
- Stop supervision only after the task is accepted, cancelled, deleted at the
  user's request, or otherwise has no possible continuation.

## Approval and completion

- `input-required` means no worker execution has begun. Explain the operation,
  target, risk, and rollback, then ask one clear approval question.
- Native edit and command requests must remain visible in agentHub WebUI and its
  interaction audit record. Never bypass the native approval flow or downgrade
  security, scan, test, or release gates to make a task pass.
- Respond through `interactions/respond` only when the pending interaction says
  `inspectable=true` and `action_intent_status=awaiting_hermes`, and only after
  checking the exact target, effect, and rollback. Use `allowed-once`, never a
  standing grant. `awaiting_user`, deletion, move, unknown or unverified
  operations, paths outside the task workspace, and missing rollback evidence
  require the user to decide in WebUI.
- `command.read` is the sole exception to the rollback-evidence requirement:
  when `risk=read`, the normalized command and argv are present in `targets`,
  the policy route is `hermes`, and `rollback_plan=null`, null means that no
  mutation occurred and rollback is not applicable. Hermes may issue exactly
  one `allowed-once` response. This currently covers only the server-recognized
  bounded Docker inspection commands. Never extend this rule to
  `command.execute`, unknown commands, shell composition, remote daemon flags,
  `docker exec`, lifecycle changes, or write/delete operations.
- Poll with `tasks/get`; do not treat the initial `submitted` response as task
  completion.
- `delivery=agent-bridge-durable` is only production-ready when the installed
  Hermes Studio runtime passes the repository compatibility check. A native
  completion that is merely pending in the durable queue is not evidence that
  the original WebUI session was awakened.
- Preserve the task ID and context ID in the conversation.
- Review reported artifacts and the agentHub task record before telling the
  user that work completed.
- A workspace does not grant write authority. DSH starts read-only. Each write
  must produce an inspectable ActionIntent; approve it only when its exact path
  stays inside the task workspace, the Agent Profile permits the operation,
  and a rollback plan exists. Otherwise escalate to the user.
- Worker completion or `awaiting_acceptance` is not formal acceptance. Hermes may
  review the result and recommend acceptance or rework, but must call
  `tasks/accept` or `tasks/request-rework` only after the user explicitly states
  that decision. Never self-accept on the user's behalf.

## Production test discipline

- Start a real dispatch test with a small, reversible, workspace-bounded task;
  state the Agent, workspace, expected change, verification, and rollback.
- Do not create duplicate sessions or tasks when a known task ID or `context_id`
  can be resumed. Report all created task, collaboration, context, watch, and
  native session IDs needed for audit or cleanup.
- Do not delete test tasks, conversations, artifacts, watches, or native sessions
  merely because a test finished. Clean them up only when the user explicitly
  requests it, and verify both agentHub records and worker-native session state.

## Failure handling

- Authentication, gateway, Registry, or offline errors are hard failures. Do
  not fall back to a direct worker CLI.
- If the user names an Agent not returned by `agents/list`, report that it is
  unavailable and ask for a decision.
- If context is lost, use the known task ID with `tasks/get`; do not create a
  duplicate task.
- If an agent-bridge notification remains pending, keep the agentHub outbox
  unacknowledged and report the runtime delivery failure. Never forge WebUI
  session state, inject through a different user's session, or mark the task
  accepted to clear the notification.
