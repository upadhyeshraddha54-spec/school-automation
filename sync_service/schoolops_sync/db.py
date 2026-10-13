"""PostgreSQL schema and connection helpers.

Design rules:
* students are never deleted by the sync (calls and payments keep pointing at them);
* fee values are stored as an append-only history (fee_snapshots), never overwritten;
* calls and messages are written only by people/the app, never by the Excel sync.
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
    id            BIGSERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    trigger       TEXT NOT NULL,             -- manual | watcher | reconcile | api
    dry_run       BOOLEAN NOT NULL,
    file_path     TEXT NOT NULL,
    file_hash     TEXT,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | dry_run | applied | failed | blocked
    summary       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS sync_issues (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    level       TEXT NOT NULL,
    code        TEXT NOT NULL,
    message     TEXT NOT NULL,
    sheet       TEXT,
    row_number  INTEGER
);

CREATE TABLE IF NOT EXISTS students (
    id              BIGSERIAL PRIMARY KEY,
    identity_key    TEXT NOT NULL UNIQUE,
    student_ext_id  TEXT,
    medium          TEXT NOT NULL,
    std             TEXT NOT NULL,
    section         TEXT NOT NULL DEFAULT '',
    sr_no           TEXT,
    name            TEXT NOT NULL,
    contact         TEXT,
    source_file     TEXT,
    source_sheet    TEXT,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    missing_since   TIMESTAMPTZ               -- set when a student vanishes from the latest file
);
CREATE INDEX IF NOT EXISTS students_group_idx ON students (medium, std, section);
CREATE INDEX IF NOT EXISTS students_name_idx ON students (lower(name));

CREATE TABLE IF NOT EXISTS fee_snapshots (
    id           BIGSERIAL PRIMARY KEY,
    student_id   BIGINT NOT NULL REFERENCES students(id),
    run_id       BIGINT REFERENCES sync_runs(id),
    source       TEXT NOT NULL DEFAULT 'excel',   -- excel | review_approved
    total_fee    NUMERIC(12,2),
    discount     NUMERIC(12,2),
    paid         NUMERIC(12,2),
    balance      NUMERIC(12,2) NOT NULL,
    raw          JSONB NOT NULL DEFAULT '{}'::jsonb,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fee_snapshots_student_idx ON fee_snapshots (student_id, id DESC);

CREATE OR REPLACE VIEW current_fees AS
SELECT DISTINCT ON (student_id) *
FROM fee_snapshots
ORDER BY student_id, id DESC;

CREATE TABLE IF NOT EXISTS calls (
    id              BIGSERIAL PRIMARY KEY,
    student_id      BIGINT NOT NULL REFERENCES students(id),
    called_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    caller          TEXT,
    outcome         TEXT NOT NULL,
    follow_up_date  DATE,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS calls_student_idx ON calls (student_id, called_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    student_id   BIGINT NOT NULL REFERENCES students(id),
    lang         TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'whatsapp',
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'mock_not_sent',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_items (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT REFERENCES sync_runs(id),
    student_id    BIGINT REFERENCES students(id),
    kind          TEXT NOT NULL,          -- fee_change | possible_same_student
    details       JSONB NOT NULL,
    details_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ,
    resolved_by   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS review_items_dedupe ON review_items (kind, details_hash);

CREATE TABLE IF NOT EXISTS file_state (
    path            TEXT PRIMARY KEY,
    last_hash       TEXT NOT NULL,
    last_synced_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_id     BIGINT REFERENCES sync_runs(id)
);
"""

ALL_TABLES = ("messages", "calls", "review_items", "fee_snapshots", "file_state",
              "students", "sync_issues", "sync_runs")


SCHOOL_TIMEZONE = "Asia/Kolkata"  # "today" for follow-ups and calls means the school's today


def connect(url: str) -> psycopg.Connection:
    # autocommit: every multi-statement change is wrapped explicitly in conn.transaction()
    return psycopg.connect(url, row_factory=dict_row, autocommit=True,
                           options=f"-c timezone={SCHOOL_TIMEZONE} -c client_encoding=UTF8")


def init_db(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(SCHEMA)
