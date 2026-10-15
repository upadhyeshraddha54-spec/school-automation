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

# Additive upgrades. Safe to run on an existing database (everything is IF NOT EXISTS).
MIGRATIONS = """
-- One parent/guardian (family) -> many students. A phone number is never used as an identity.
CREATE TABLE IF NOT EXISTS families (
    id            BIGSERIAL PRIMARY KEY,
    family_key    TEXT UNIQUE,              -- verified ID from Excel (e.g. a Family/Parent ID column), if mapped
    display_name  TEXT,                     -- parent name if Excel has one
    verified      BOOLEAN NOT NULL DEFAULT FALSE,  -- true when linked by a verified ID or confirmed by staff
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE students ADD COLUMN IF NOT EXISTS family_id BIGINT REFERENCES families(id);
ALTER TABLE students ADD COLUMN IF NOT EXISTS parent_name TEXT;
ALTER TABLE students ADD COLUMN IF NOT EXISTS family_ext_id TEXT;
CREATE INDEX IF NOT EXISTS students_family_idx ON students (family_id);

-- One call action can cover several siblings: one row per child, same call_group.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS call_group TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS promised_amount NUMERIC(12,2);
UPDATE calls SET call_group = 'c' || id WHERE call_group IS NULL;
CREATE INDEX IF NOT EXISTS calls_group_idx ON calls (call_group);

CREATE TABLE IF NOT EXISTS follow_ups (
    id                 BIGSERIAL PRIMARY KEY,
    student_id         BIGINT NOT NULL REFERENCES students(id),
    kind               TEXT NOT NULL,     -- promise | part_payment | callback | retry | dispute | verify_payment | fix_number
    due_date           DATE,
    status             TEXT NOT NULL DEFAULT 'open',  -- open | done | missed | paid | cancelled
    source_call_id     BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    closed_by_call_id  BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    balance_at_open    NUMERIC(12,2),
    promised_amount    NUMERIC(12,2),
    note               TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at          TIMESTAMPTZ,
    close_reason       TEXT
);
-- never two open follow-ups for the same child
CREATE UNIQUE INDEX IF NOT EXISTS follow_ups_one_open ON follow_ups (student_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS campaigns (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    filters       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- medium / std / section
    daily_target  INTEGER NOT NULL DEFAULT 40,
    status        TEXT NOT NULL DEFAULT 'active',       -- active | closed
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  JSONB NOT NULL
);

-- Payments seen in Excel: one row per fee snapshot at most (never counted twice)
CREATE TABLE IF NOT EXISTS payments (
    id           BIGSERIAL PRIMARY KEY,
    student_id   BIGINT NOT NULL REFERENCES students(id),
    snapshot_id  BIGINT NOT NULL UNIQUE REFERENCES fee_snapshots(id),
    amount       NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    basis        TEXT NOT NULL,          -- 'paid_increased' | 'balance_decreased'
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_student_idx ON payments (student_id, detected_at);

-- OneDrive (Microsoft Graph) files mirrored read-only into the local folder
CREATE TABLE IF NOT EXISTS onedrive_items (
    item_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    ctag          TEXT,
    size          BIGINT,
    modified_at   TIMESTAMPTZ,
    downloaded_at TIMESTAMPTZ,
    deleted_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS message_drafts (
    id            BIGSERIAL PRIMARY KEY,
    family_id     BIGINT REFERENCES families(id),
    student_ids   BIGINT[] NOT NULL,
    lang          TEXT NOT NULL,
    body          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft | approved | discarded  (nothing is ever sent)
    edited        BOOLEAN NOT NULL DEFAULT FALSE,
    left_out      JSONB NOT NULL DEFAULT '[]'::jsonb,  -- children not included and why (no verified amount, nothing due)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at   TIMESTAMPTZ,
    approved_by   TEXT
);
ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'manual';  -- manual | whatsapp | sms
ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS reason TEXT;          -- which list it came from
ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS batch_id TEXT;
ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS template_name TEXT;   -- WhatsApp approved template / SMS DLT template
ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS segments INTEGER;     -- SMS parts
ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS created_on DATE NOT NULL DEFAULT current_date;
-- at most one live WhatsApp and one live SMS draft per parent per day
CREATE UNIQUE INDEX IF NOT EXISTS drafts_one_per_family_day ON message_drafts (family_id, channel, created_on)
    WHERE status <> 'discarded' AND channel IN ('whatsapp', 'sms') AND family_id IS NOT NULL;
"""

ALL_TABLES = ("payments", "onedrive_items", "message_drafts", "follow_ups", "campaigns", "settings", "messages", "calls", "review_items",
              "fee_snapshots", "file_state", "students", "families", "sync_issues", "sync_runs")


SCHOOL_TIMEZONE = "Asia/Kolkata"  # "today" for follow-ups and calls means the school's today


def connect(url: str) -> psycopg.Connection:
    # autocommit: every multi-statement change is wrapped explicitly in conn.transaction()
    return psycopg.connect(url, row_factory=dict_row, autocommit=True,
                           options=f"-c timezone={SCHOOL_TIMEZONE} -c client_encoding=UTF8")


SYNC_LOCK = 7_310_001  # one sync applies at a time (watcher, OneDrive poller, uploads, reconcile)


def init_db(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7310002)")  # two app starts can't migrate at once
        conn.execute(SCHEMA)
        conn.execute(MIGRATIONS)
    from .families import ensure_families  # local import: families uses this module
    ensure_families(conn)
