-- Fence delayed lifecycle events from an earlier dispatch of the same task.
ALTER TABLE tasks ADD COLUMN execution_generation TEXT;
