-- =============================================================================
-- 02_migration_tracking.sql
-- Migration Tracking Database Schema
-- Tracks job state, record-level status, checkpoints, and audit events
-- =============================================================================

\c sfmigration

-- Drop in reverse FK order
DROP TABLE IF EXISTS migration_audit_events CASCADE;
DROP TABLE IF EXISTS migration_record_errors CASCADE;
DROP TABLE IF EXISTS migration_batch_jobs CASCADE;
DROP TABLE IF EXISTS migration_checkpoints CASCADE;
DROP TABLE IF EXISTS migration_runs CASCADE;

-- ---------------------------------------------------------------------------
-- Migration Runs (top-level job)
-- ---------------------------------------------------------------------------
CREATE TABLE migration_runs (
    run_id              VARCHAR(50)     PRIMARY KEY,
    run_name            VARCHAR(255)    NOT NULL,
    environment         VARCHAR(20)     NOT NULL DEFAULT 'development',  -- development, staging, production
    source_system       VARCHAR(50)     NOT NULL DEFAULT 'SIEBEL_8_1',

    -- Record type scope
    object_types        TEXT[]          NOT NULL,                        -- {Account, Contact, Opportunity}
    total_records       INTEGER         DEFAULT 0,
    processed_records   INTEGER         DEFAULT 0,
    successful_records  INTEGER         DEFAULT 0,
    failed_records      INTEGER         DEFAULT 0,
    skipped_records     INTEGER         DEFAULT 0,

    -- Status
    status              VARCHAR(20)     NOT NULL DEFAULT 'PENDING',
    -- PENDING, VALIDATING, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED, ROLLED_BACK
    error_rate          NUMERIC(5,2)    DEFAULT 0.0,
    error_threshold     NUMERIC(5,2)    DEFAULT 5.0,
    is_dry_run          BOOLEAN         DEFAULT TRUE,

    -- Configuration
    batch_size          INTEGER         DEFAULT 50,
    max_retries         INTEGER         DEFAULT 3,
    parallelism         SMALLINT        DEFAULT 2,

    -- Validation
    validation_status   VARCHAR(20),    -- PENDING, PASS, FAIL, BLOCKED
    validation_score    NUMERIC(4,3),   -- 0.000 – 1.000
    validation_grade    CHAR(1),        -- A, B, C, D, F
    security_gate       VARCHAR(10),    -- PASS, FAIL, BLOCKED

    -- Timing
    started_at          TIMESTAMPTZ,
    paused_at           TIMESTAMPTZ,
    resumed_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    estimated_eta       TIMESTAMPTZ,

    -- SF target
    sf_org_id           VARCHAR(18),
    sf_instance_url     VARCHAR(255),

    -- Metadata
    initiated_by        VARCHAR(100)    DEFAULT 'system',
    notes               TEXT,
    tags                TEXT[],
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_migration_runs_status  ON migration_runs(status);
CREATE INDEX idx_migration_runs_env     ON migration_runs(environment);
CREATE INDEX idx_migration_runs_created ON migration_runs(created_at DESC);

-- ---------------------------------------------------------------------------
-- Batch Jobs (sub-tasks per object type)
-- ---------------------------------------------------------------------------
CREATE TABLE migration_batch_jobs (
    batch_id            VARCHAR(50)     PRIMARY KEY,
    run_id              VARCHAR(50)     NOT NULL REFERENCES migration_runs(run_id),
    object_type         VARCHAR(50)     NOT NULL,   -- Account, Contact, Opportunity
    batch_number        INTEGER         NOT NULL,
    batch_size          INTEGER         NOT NULL,

    -- Record range
    offset_start        INTEGER         NOT NULL DEFAULT 0,
    offset_end          INTEGER         NOT NULL DEFAULT 0,

    -- Status
    status              VARCHAR(20)     NOT NULL DEFAULT 'PENDING',
    -- PENDING, RUNNING, COMPLETED, FAILED, RETRYING
    records_processed   INTEGER         DEFAULT 0,
    records_succeeded   INTEGER         DEFAULT 0,
    records_failed      INTEGER         DEFAULT 0,
    attempt_number      SMALLINT        DEFAULT 1,

    -- SF Bulk API Job
    sf_job_id           VARCHAR(18),
    sf_job_state        VARCHAR(20),

    -- Timing
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    duration_ms         INTEGER,

    -- Error
    error_message       TEXT,
    error_code          VARCHAR(50),

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, object_type, batch_number, attempt_number)
);

CREATE INDEX idx_batch_jobs_run         ON migration_batch_jobs(run_id);
CREATE INDEX idx_batch_jobs_status      ON migration_batch_jobs(status);
CREATE INDEX idx_batch_jobs_object      ON migration_batch_jobs(object_type);

-- ---------------------------------------------------------------------------
-- Checkpoints (for resume after failure)
-- ---------------------------------------------------------------------------
CREATE TABLE migration_checkpoints (
    checkpoint_id       SERIAL          PRIMARY KEY,
    run_id              VARCHAR(50)     NOT NULL REFERENCES migration_runs(run_id),
    object_type         VARCHAR(50)     NOT NULL,
    last_processed_id   VARCHAR(40)     NOT NULL,   -- Last legacy record ID processed
    records_completed   INTEGER         NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, object_type)
);

-- ---------------------------------------------------------------------------
-- Record-level errors
-- ---------------------------------------------------------------------------
CREATE TABLE migration_record_errors (
    error_id            BIGSERIAL       PRIMARY KEY,
    run_id              VARCHAR(50)     NOT NULL REFERENCES migration_runs(run_id),
    batch_id            VARCHAR(50)     REFERENCES migration_batch_jobs(batch_id),
    object_type         VARCHAR(50)     NOT NULL,
    legacy_record_id    VARCHAR(40)     NOT NULL,
    sf_error_code       VARCHAR(50),
    sf_error_message    TEXT,
    field_name          VARCHAR(100),   -- If field-level error
    attempted_value     TEXT,           -- Value that caused the error (sanitized)
    is_retryable        BOOLEAN         DEFAULT TRUE,
    retry_count         SMALLINT        DEFAULT 0,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_record_errors_run      ON migration_record_errors(run_id);
CREATE INDEX idx_record_errors_object   ON migration_record_errors(object_type);
CREATE INDEX idx_record_errors_legacy   ON migration_record_errors(legacy_record_id);
CREATE INDEX idx_record_errors_retry    ON migration_record_errors(is_retryable, retry_count);

-- ---------------------------------------------------------------------------
-- Audit events (agent decisions, gate outcomes, human approvals)
-- ---------------------------------------------------------------------------
CREATE TABLE migration_audit_events (
    event_id            BIGSERIAL       PRIMARY KEY,
    run_id              VARCHAR(50)     REFERENCES migration_runs(run_id),
    agent_name          VARCHAR(50),
    event_type          VARCHAR(50)     NOT NULL,
    -- AGENT_INVOCATION, GATE_DECISION, HUMAN_APPROVAL, TOOL_CALL,
    -- SECURITY_BLOCK, VALIDATION_RESULT, MIGRATION_ACTION
    severity            VARCHAR(10)     DEFAULT 'INFO', -- DEBUG, INFO, WARNING, ERROR, CRITICAL
    summary             TEXT            NOT NULL,
    details             JSONB,
    entry_hash          VARCHAR(64),    -- HMAC chain link
    prev_hash           VARCHAR(64),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_run              ON migration_audit_events(run_id);
CREATE INDEX idx_audit_event_type       ON migration_audit_events(event_type);
CREATE INDEX idx_audit_severity         ON migration_audit_events(severity);
CREATE INDEX idx_audit_created          ON migration_audit_events(created_at DESC);

-- ---------------------------------------------------------------------------
-- Helper view: migration run summary
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_migration_run_summary AS
SELECT
    r.run_id,
    r.run_name,
    r.status,
    r.environment,
    r.is_dry_run,
    r.total_records,
    r.successful_records,
    r.failed_records,
    ROUND(r.error_rate, 2)                                      AS error_rate_pct,
    r.validation_grade,
    ARRAY_TO_STRING(r.object_types, ', ')                       AS object_types,
    r.started_at,
    r.completed_at,
    EXTRACT(EPOCH FROM (COALESCE(r.completed_at, NOW()) - r.started_at))::INTEGER AS duration_seconds,
    COUNT(DISTINCT b.batch_id)                                  AS total_batches,
    COUNT(DISTINCT b.batch_id) FILTER (WHERE b.status = 'COMPLETED') AS completed_batches,
    COUNT(DISTINCT e.error_id)                                  AS distinct_error_types
FROM migration_runs r
LEFT JOIN migration_batch_jobs b ON b.run_id = r.run_id
LEFT JOIN migration_record_errors e ON e.run_id = r.run_id
GROUP BY r.run_id
ORDER BY r.created_at DESC;

COMMENT ON TABLE migration_runs     IS 'Top-level migration job tracking — one row per migration run';
COMMENT ON TABLE migration_batch_jobs IS 'Batch-level tracking — one row per Salesforce Bulk API job submission';
COMMENT ON TABLE migration_checkpoints IS 'Resume checkpoints — allows picking up after failure';
COMMENT ON TABLE migration_record_errors IS 'Record-level errors from Salesforce Bulk API 2.0 failure responses';
COMMENT ON TABLE migration_audit_events IS 'Immutable agent audit trail — HMAC-chained for tamper detection';
