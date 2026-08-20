-- Migration 009 (PostgreSQL): structured Hermes Task Plans

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_step_id TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_context_json TEXT;

CREATE TABLE IF NOT EXISTS task_plans (
    id TEXT PRIMARY KEY,
    collaboration_id TEXT NOT NULL REFERENCES collaborations(id),
    revision INTEGER NOT NULL,
    objective TEXT NOT NULL,
    project TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    based_on_revision INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (collaboration_id, revision)
);

CREATE TABLE IF NOT EXISTS task_plan_steps (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES task_plans(id),
    step_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    profile_id TEXT NOT NULL REFERENCES agent_profiles(id),
    profile_version INTEGER NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    expected_artifacts_json TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    expected_operations_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, step_key)
);

CREATE INDEX IF NOT EXISTS task_plans_collaboration_idx
    ON task_plans(collaboration_id, revision);
CREATE INDEX IF NOT EXISTS task_plan_steps_plan_idx
    ON task_plan_steps(plan_id, ordinal);
CREATE INDEX IF NOT EXISTS task_plan_steps_agent_idx
    ON task_plan_steps(agent_id, status);
CREATE INDEX IF NOT EXISTS tasks_plan_step_idx ON tasks(plan_step_id);
