-- Migration 005: versioned Agent Template/Profile configuration

ALTER TABLE agents ADD COLUMN template_id TEXT;
ALTER TABLE agents ADD COLUMN profile_id TEXT;

CREATE TABLE agent_templates (
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

CREATE TABLE agent_profiles (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
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
    reviewer_profile_id TEXT,
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
    updated_at TEXT NOT NULL,
    FOREIGN KEY (template_id) REFERENCES agent_templates(id),
    FOREIGN KEY (reviewer_profile_id) REFERENCES agent_profiles(id)
);

CREATE TABLE agent_profile_versions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES agent_profiles(id),
    UNIQUE (profile_id, version)
);

CREATE INDEX agent_profiles_template_idx ON agent_profiles(template_id, status);
CREATE INDEX agent_profiles_project_idx ON agent_profiles(project, status);
CREATE INDEX agent_profile_versions_idx ON agent_profile_versions(profile_id, version);
CREATE INDEX agents_profile_idx ON agents(profile_id);
