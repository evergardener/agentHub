---
name: agenthub-orchestration
description: Route delegated Agent work through agentHub.
version: 1.0.0
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
{"agenthub":"v1","action":"tasks/create","agent":"codex","objective":"full unsummarized instruction","project":"optional project"}
```

Query a task:

```json
{"agenthub":"v1","action":"tasks/get","task_id":"T-..."}
```

Every task response includes `task_id=T-...` in the visible status message.
Capture that exact ID immediately; Hermes' native A2A renderer may omit the
structured `task.id` field even though agentHub returned it.

Approve or reject only after the user decision is clear:

```json
{"agenthub":"v1","action":"tasks/approve","task_id":"T-..."}
```

```json
{"agenthub":"v1","action":"tasks/reject","task_id":"T-..."}
```

## Disabled Agent behavior

If discovery or task creation reports `agent disabled`, do not probe, create,
delegate, retry, or silently substitute another Agent. Ask the user whether to
enable and re-discover that Agent, or choose a currently enabled Agent.

## Task instruction quality

The `objective` is the actual instruction preserved in agentHub. Include the
full goal, current context, constraints, expected artifacts, risky operations,
acceptance criteria, and what must be discussed with Hermes before execution.
Do not replace it with a one-line summary.

## Approval and completion

- `input-required` means no worker execution has begun. Explain the operation,
  target, risk, and rollback, then ask one clear approval question.
- Poll with `tasks/get`; do not treat the initial `submitted` response as task
  completion.
- Preserve the task ID and context ID in the conversation.
- Review reported artifacts and the agentHub task record before telling the
  user that work completed.

## Failure handling

- Authentication, gateway, Registry, or offline errors are hard failures. Do
  not fall back to a direct worker CLI.
- If the user names an Agent not returned by `agents/list`, report that it is
  unavailable and ask for a decision.
- If context is lost, use the known task ID with `tasks/get`; do not create a
  duplicate task.
