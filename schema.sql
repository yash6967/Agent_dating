PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    identity_score REAL,
    identity_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_active_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_sources (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('linkedin', 'instagram')),
    url TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    last_checked_at TEXT,
    last_changed_at TEXT,
    next_due_at TEXT,
    error TEXT,
    UNIQUE(person_id, platform)
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES profile_sources(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    apify_run_id TEXT,
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_versions (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    linkedin_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
    instagram_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
    generated_at TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    summary TEXT NOT NULL,
    unknowns_json TEXT NOT NULL,
    UNIQUE(person_id, linkedin_snapshot_id, instagram_snapshot_id, analyzer)
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL UNIQUE REFERENCES people(id) ON DELETE CASCADE,
    current_profile_version_id TEXT NOT NULL REFERENCES profile_versions(id),
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_claims (
    id TEXT PRIMARY KEY,
    profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (category IN ('interest', 'hobby', 'need', 'value', 'style', 'work')),
    claim TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE TABLE IF NOT EXISTS agent_dates (
    id TEXT PRIMARY KEY,
    person_a_id TEXT NOT NULL REFERENCES people(id),
    person_b_id TEXT NOT NULL REFERENCES people(id),
    profile_a_id TEXT NOT NULL REFERENCES profile_versions(id),
    profile_b_id TEXT NOT NULL REFERENCES profile_versions(id),
    created_at TEXT NOT NULL,
    engine TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    evaluation_json TEXT,
    CHECK(person_a_id <> person_b_id)
);

CREATE TABLE IF NOT EXISTS date_turns (
    id TEXT PRIMARY KEY,
    date_id TEXT NOT NULL REFERENCES agent_dates(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    speaker_id TEXT NOT NULL REFERENCES people(id),
    phase TEXT NOT NULL,
    text TEXT NOT NULL,
    evidence_keys_json TEXT NOT NULL,
    UNIQUE(date_id, turn_index)
);

CREATE TABLE IF NOT EXISTS ranking_runs (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    generated_at TEXT NOT NULL,
    profile_version_id TEXT NOT NULL REFERENCES profile_versions(id),
    algorithm TEXT NOT NULL,
    cohort_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ranking_items (
    run_id TEXT NOT NULL REFERENCES ranking_runs(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES people(id),
    position INTEGER NOT NULL,
    score REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    date_id TEXT REFERENCES agent_dates(id),
    PRIMARY KEY(run_id, candidate_id),
    UNIQUE(run_id, position)
);

CREATE INDEX IF NOT EXISTS idx_sources_due ON profile_sources(next_due_at, platform);
CREATE INDEX IF NOT EXISTS idx_snapshots_latest ON source_snapshots(source_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_latest ON profile_versions(person_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dates_pair ON agent_dates(person_a_id, person_b_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rankings_latest ON ranking_runs(person_id, generated_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS profile_search USING fts5(person_id UNINDEXED, document);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO app_meta(key, value) VALUES('cohort_version', 0);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('analyze_cohort', 'date_cohort')),
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT NOT NULL DEFAULT 'queued',
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS autonomous_date_plan (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    person_a_id TEXT NOT NULL REFERENCES people(id),
    person_b_id TEXT NOT NULL REFERENCES people(id),
    priority INTEGER NOT NULL,
    pre_score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    date_id TEXT REFERENCES agent_dates(id),
    error TEXT,
    UNIQUE(run_id, person_a_id, person_b_id),
    UNIQUE(run_id, priority)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_date_plan_run ON autonomous_date_plan(run_id, priority);
