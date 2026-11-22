-- Postgres schema for Fieldnote.
-- The desk you run with `uv run fieldnote` uses SQLite and does not enforce
-- these policies. Load this file into Postgres (docker compose does) when you
-- want row-level security. Reviewers read their project and insert approvals.
-- Only the service role writes tasks.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  project_name TEXT NOT NULL,
  region TEXT,
  start_date DATE,
  target_end_date DATE,
  contract_type TEXT
);

CREATE TABLE IF NOT EXISTS project_members (
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('reviewer', 'owner')),
  PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  task_title TEXT NOT NULL,
  owner_id TEXT,
  owner_name TEXT,
  planned_due DATE,
  status TEXT,
  origin TEXT NOT NULL DEFAULT 'seed'
);

CREATE TABLE IF NOT EXISTS source_documents (
  document_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT,
  body TEXT,
  happened_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS source_chunks (
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES source_documents(document_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  source_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  embedding vector(64)
);

CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  proposal_id TEXT,
  decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
  note TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS change_log (
  change_id TEXT PRIMARY KEY,
  dedupe_hash TEXT UNIQUE NOT NULL,
  approval_id TEXT REFERENCES approvals(approval_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  task_id TEXT,
  field_name TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;
ALTER TABLE project_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_members FORCE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE source_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_documents FORCE ROW LEVEL SECURITY;
ALTER TABLE source_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
ALTER TABLE change_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE change_log FORCE ROW LEVEL SECURITY;

-- current_setting(..., true) returns NULL instead of throwing when unset.
CREATE OR REPLACE FUNCTION fieldnote_uid() RETURNS TEXT
LANGUAGE sql STABLE AS $$
  SELECT current_setting('request.jwt.claim.sub', true)
$$;

CREATE OR REPLACE FUNCTION fieldnote_is_service() RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
  SELECT current_setting('request.jwt.claim.role', true) = 'service_role'
$$;

CREATE OR REPLACE FUNCTION fieldnote_is_member(pid TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM project_members m
    WHERE m.project_id = pid AND m.user_id = fieldnote_uid()
  )
$$;

CREATE POLICY projects_read ON projects
  FOR SELECT
  USING (fieldnote_is_service() OR fieldnote_is_member(project_id));

CREATE POLICY members_read ON project_members
  FOR SELECT
  USING (fieldnote_is_service() OR user_id = fieldnote_uid());

CREATE POLICY tasks_read ON tasks
  FOR SELECT
  USING (fieldnote_is_service() OR fieldnote_is_member(project_id));

CREATE POLICY tasks_write_service ON tasks
  FOR ALL
  USING (fieldnote_is_service())
  WITH CHECK (fieldnote_is_service());

CREATE POLICY documents_read ON source_documents
  FOR SELECT
  USING (fieldnote_is_service() OR fieldnote_is_member(project_id));

CREATE POLICY chunks_read ON source_chunks
  FOR SELECT
  USING (fieldnote_is_service() OR fieldnote_is_member(project_id));

CREATE POLICY documents_write_service ON source_documents
  FOR ALL
  USING (fieldnote_is_service())
  WITH CHECK (fieldnote_is_service());

CREATE POLICY chunks_write_service ON source_chunks
  FOR ALL
  USING (fieldnote_is_service())
  WITH CHECK (fieldnote_is_service());

CREATE POLICY approvals_read ON approvals
  FOR SELECT
  USING (fieldnote_is_service() OR fieldnote_is_member(project_id));

CREATE POLICY approvals_insert_reviewer ON approvals
  FOR INSERT
  WITH CHECK (
    fieldnote_is_member(project_id)
    AND created_by = fieldnote_uid()
  );

CREATE POLICY change_log_read ON change_log
  FOR SELECT
  USING (fieldnote_is_service() OR fieldnote_is_member(project_id));

CREATE POLICY change_log_write_service ON change_log
  FOR INSERT
  WITH CHECK (fieldnote_is_service());
