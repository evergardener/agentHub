-- Per-task model selection remains bounded by a versioned Agent Profile.

ALTER TABLE agent_profiles
    ADD COLUMN IF NOT EXISTS allowed_models_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE agent_profiles
    ADD COLUMN IF NOT EXISTS reasoning_effort TEXT;
ALTER TABLE agent_profiles
    ADD COLUMN IF NOT EXISTS allowed_reasoning_efforts_json TEXT NOT NULL DEFAULT '[]';

UPDATE agent_profiles
SET allowed_models_json = '["gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna","gpt-5.5","gpt-5.4"]',
    allowed_reasoning_efforts_json = '["none","low","medium","high","xhigh","max"]'
WHERE id = 'AP-CODEX-BACKEND';
