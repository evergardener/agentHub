-- Migration 009: structured Hermes Task Plans bound to Agent Profiles

ALTER TABLE tasks ADD COLUMN plan_step_id TEXT;
ALTER TABLE tasks ADD COLUMN plan_context_json TEXT;

CREATE TABLE task_plans (
    id TEXT PRIMARY KEY,
    collaboration_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    objective TEXT NOT NULL,
    project TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    based_on_revision INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id),
    UNIQUE (collaboration_id, revision)
);

CREATE TABLE task_plan_steps (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    expected_artifacts_json TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    expected_operations_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES task_plans(id),
    FOREIGN KEY (profile_id) REFERENCES agent_profiles(id),
    UNIQUE (plan_id, step_key),
    UNIQUE (task_id)
);

CREATE INDEX task_plans_collaboration_idx
    ON task_plans(collaboration_id, revision);
CREATE INDEX task_plan_steps_plan_idx ON task_plan_steps(plan_id, ordinal);
CREATE INDEX task_plan_steps_agent_idx ON task_plan_steps(agent_id, status);
CREATE INDEX tasks_plan_step_idx ON tasks(plan_step_id);
