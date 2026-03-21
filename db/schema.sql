-- =============================================================================
-- Apex Ledger — Event Store Schema
-- PostgreSQL schema for append-only event sourcing infrastructure
-- =============================================================================

-- Core append-only event log
-- Every fact in the system is stored as an immutable row in this table.
-- No UPDATE or DELETE is ever performed on this table.
CREATE TABLE IF NOT EXISTS events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id        TEXT NOT NULL,
    stream_position  BIGINT NOT NULL,
    global_position  BIGINT GENERATED ALWAYS AS IDENTITY,
    event_type       TEXT NOT NULL,
    event_version    SMALLINT NOT NULL DEFAULT 1,
    payload          JSONB NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

    -- Enforces: one event per (stream, position) — the foundation of OCC
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

-- Stream-scoped reads (aggregate replay)
CREATE INDEX IF NOT EXISTS idx_events_stream_id ON events (stream_id, stream_position);
-- Global ordering (projection catch-up)
CREATE INDEX IF NOT EXISTS idx_events_global_pos ON events (global_position);
-- Type-filtered queries (e.g. "all CreditAnalysisCompleted events")
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
-- Temporal queries (regulatory time-travel)
CREATE INDEX IF NOT EXISTS idx_events_recorded ON events (recorded_at);

-- Stream metadata — tracks the current (latest) version of each stream
-- Used for optimistic concurrency checks and stream lifecycle management
CREATE TABLE IF NOT EXISTS event_streams (
    stream_id        TEXT PRIMARY KEY,
    aggregate_type   TEXT NOT NULL,
    current_version  BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at      TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Projection checkpoints — tracks how far each named projection
-- has processed through the global event log
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name  TEXT PRIMARY KEY,
    last_position    BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Outbox — guaranteed at-least-once delivery to downstream consumers
-- Written in the same transaction as event appends, polled by a relay process
CREATE TABLE IF NOT EXISTS outbox (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID NOT NULL REFERENCES events(event_id),
    destination      TEXT NOT NULL,
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at     TIMESTAMPTZ,
    attempts         SMALLINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox (created_at)
    WHERE published_at IS NULL;

-- Snapshot table — stores periodic aggregate state snapshots
-- Used to avoid replaying entire streams for long-lived aggregates
CREATE TABLE IF NOT EXISTS snapshots (
    stream_id        TEXT NOT NULL,
    version          BIGINT NOT NULL,
    snapshot_type    TEXT NOT NULL,
    state            JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (stream_id, version)
);
