## agentHub delegation boundary

For any request that should be delegated to Codex, DSH, Kimi, Pi, or another
worker Agent, use the `agenthub-orchestration` skill and the single configured
A2A peer `agenthub`. Always discover through agentHub first. Do not execute a
worker CLI, headless profile, or direct adapter call as a fallback. A direct
worker invocation is permitted only after the user explicitly requests and
confirms bypassing agentHub, with the consequence that agentHub will not track
that run. Reuse the same A2A context ID for multi-turn collaboration. If a
requested Agent is disabled, ask whether to enable and re-discover it or choose
another enabled Agent; never probe, delegate, retry, or silently substitute it.
Capture the `task_id=T-...` value from every visible agentHub task status and
use that exact ID for approval, rejection, and `tasks/get` polling.
