-- Migration 005 (PostgreSQL): versioned Agent Template/Profile configuration

ALTER TABLE agents ADD COLUMN IF NOT EXISTS template_id TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS profile_id TEXT;

CREATE TABLE IF NOT EXISTS agent_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    adapter_kind TEXT NOT NULL,
    description TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    default_config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES agent_templates(id),
    name TEXT NOT NULL,
    project TEXT,
    role_prompt TEXT,
    responsibilities_json TEXT NOT NULL DEFAULT '[]',
    execution_mode TEXT NOT NULL DEFAULT 'read_only',
    allowed_operations_json TEXT NOT NULL DEFAULT '[]',
    denied_operations_json TEXT NOT NULL DEFAULT '[]',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    workspace_roots_json TEXT NOT NULL DEFAULT '[]',
    task_types_json TEXT NOT NULL DEFAULT '[]',
    reviewer_profile_id TEXT REFERENCES agent_profiles(id),
    model TEXT,
    cost_limit_json TEXT,
    priority INTEGER NOT NULL DEFAULT 50,
    timeout_seconds INTEGER NOT NULL DEFAULT 3600,
    max_concurrent_tasks INTEGER NOT NULL DEFAULT 1,
    approval_level TEXT NOT NULL DEFAULT 'hermes',
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profile_versions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES agent_profiles(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (profile_id, version)
);

CREATE INDEX IF NOT EXISTS agent_profiles_template_idx ON agent_profiles(template_id, status);
CREATE INDEX IF NOT EXISTS agent_profiles_project_idx ON agent_profiles(project, status);
CREATE INDEX IF NOT EXISTS agent_profile_versions_idx ON agent_profile_versions(profile_id, version);
CREATE INDEX IF NOT EXISTS agents_profile_idx ON agents(profile_id);
