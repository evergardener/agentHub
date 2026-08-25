# Project agent collaboration

- For complex work that divides into independent, verifiable subtasks, the
  primary Codex agent may create subagents without requesting per-task user
  approval.
- Use `gpt-5.6-luna` with reasoning effort `max` for those subagents unless the
  user explicitly requests a different supported runtime for a specific task.
- Subagent authorization does not expand filesystem, approval, commit, push,
  deployment, deletion, credential, or external-side-effect authority. The
  primary agent remains responsible for reviewing and verifying all results.
