"""
SQLite schema, initialisation, and migration for Sieger inspection results.

File location: sieger_data/sieger.db  (data_root from config.json)
Never committed to git — machine-specific data.

Identity system:
- material_id: PLC numeric code (e.g. 8) — maps to a master_id + dimensions
- master_id:   Human name for a yarn pattern (e.g. "Blue_Diamond") — tube/dimension teaching key
- teaching_id: UUID for a specific teaching session — enables audit trail and rollback

See docs/identity_system.md for full explanation.

Usage:
    from src.db.schema import init_db
    conn = init_db("/home/msiegerips/sieger_data/sieger.db")
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Increment this when the schema changes — triggers migration
SCHEMA_VERSION = 10

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY
);

-- Teaching sessions — one row per teaching event per module.
-- scope_key = master_id for tube/dimension, "global" for stain/uv/tail.
-- See docs/identity_system.md for full design rationale.
CREATE TABLE IF NOT EXISTS teaching_sessions (
    teaching_id   TEXT    PRIMARY KEY,          -- UUID
    module        TEXT    NOT NULL,             -- tube | stain | dimension | uv | tail
    scope_key     TEXT    NOT NULL,             -- master_id or "global"
    status        TEXT    NOT NULL DEFAULT 'training',  -- training | active | superseded | failed
    created_at    TEXT    NOT NULL,             -- ISO-8601 UTC
    completed_at  TEXT,                         -- NULL until training done
    n_samples     INTEGER,                      -- number of training images used
    model_path    TEXT,                         -- path to .npz or patchcore model.pt
    threshold     REAL,                         -- computed threshold (tube: color_threshold, stain: anomaly score cutoff)
    extend_count  INTEGER NOT NULL DEFAULT 0,   -- times /extend was called (tube only, max 3)
    notes         TEXT,                         -- operator notes or error messages
    validation_json TEXT                          -- JSON validation report after teaching
);

CREATE INDEX IF NOT EXISTS idx_teaching_module_scope  ON teaching_sessions (module, scope_key, status);
CREATE INDEX IF NOT EXISTS idx_teaching_status        ON teaching_sessions (status);

-- Inspection results — one row per cone.
-- tube_teaching_id / stain_teaching_id reference which teaching was active at inspection time.
CREATE TABLE IF NOT EXISTS inspections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,           -- ISO-8601 UTC
    material_id         TEXT    NOT NULL,           -- PLC material number (string)
    master_id           TEXT,                       -- tube pattern class name (from recipe)
    basket_no           INTEGER,
    loader_id           INTEGER,
    sample_counter      INTEGER,
    result_code         INTEGER NOT NULL,           -- 1=Good 2=Defect 3=Error
    defect_type         TEXT,                       -- comma-separated defect names or "Good"
    cone_dia_mm         REAL,
    tube_dia_mm         REAL,
    stain_score         REAL,
    stain_ok            INTEGER,                    -- 1=pass 0=fail NULL=not run
    uv_radial_dip       REAL,
    uv_ok               INTEGER,
    tail_confidence     REAL,
    tail_ok             INTEGER,
    tube_pattern        TEXT,                       -- nearest class name
    tube_distance       REAL,                       -- combined distance
    tube_ok             INTEGER,
    trial_mode          INTEGER NOT NULL DEFAULT 0, -- 1=trial run
    audit_image         TEXT,                       -- filename in sieger_data/audit/YYYY-MM-DD/ (VL, annotated)
    audit_image_uv      TEXT,                       -- filename in sieger_data/audit/YYYY-MM-DD/ (UV raw, NULL if not captured this cone) (v8)
    audit_image_tail    TEXT,                       -- filename in sieger_data/audit/YYYY-MM-DD/ (Tail raw, NULL if not captured this cone) (v8)
    tube_teaching_id    TEXT,                       -- FK → teaching_sessions.teaching_id
    stain_teaching_id   TEXT,                       -- FK → teaching_sessions.teaching_id
    uv_teaching_id      TEXT,                       -- FK → teaching_sessions.teaching_id (threshold config version)
    tail_teaching_id    TEXT                        -- FK → teaching_sessions.teaching_id (YOLO weights version)
);

CREATE INDEX IF NOT EXISTS idx_inspections_timestamp    ON inspections (timestamp);
CREATE INDEX IF NOT EXISTS idx_inspections_material_id  ON inspections (material_id);
CREATE INDEX IF NOT EXISTS idx_inspections_result_code  ON inspections (result_code);

-- Capture sessions — audit trail for every data capture event.
CREATE TABLE IF NOT EXISTS capture_sessions (
    session_id      TEXT    PRIMARY KEY,          -- UUID
    module          TEXT    NOT NULL,             -- tube | stain | uv | tail | dimension
    material_ids    TEXT    NOT NULL,             -- JSON array e.g. ["1234","5678"]
    started_at      TEXT    NOT NULL,             -- ISO-8601 UTC
    stopped_at      TEXT,                         -- NULL if still active
    images_saved    INTEGER NOT NULL DEFAULT 0,   -- running count updated per frame
    stopped_by      TEXT,                         -- 'operator' | 'plc_stop' | 'system'
    operator_id     TEXT,                         -- operator who started the session (v7)
    capture_status  TEXT    NOT NULL DEFAULT 'OPEN'  -- OPEN | PENDING_REVIEW | UPLOADED (v7)
);

CREATE INDEX IF NOT EXISTS idx_capture_sessions_module     ON capture_sessions (module);
CREATE INDEX IF NOT EXISTS idx_capture_sessions_started_at ON capture_sessions (started_at);

-- Captured images — one row per cone frame saved during capture mode.
CREATE TABLE IF NOT EXISTS captured_images (
    image_id      TEXT    PRIMARY KEY,          -- UUID
    session_id    TEXT    NOT NULL,             -- FK → capture_sessions.session_id
    material_id   TEXT    NOT NULL,
    module        TEXT    NOT NULL,
    captured_at   TEXT    NOT NULL,             -- ISO-8601 UTC
    vl_path       TEXT,                         -- relative to sieger_data/
    uv_path       TEXT,
    tail_path     TEXT,
    FOREIGN KEY (session_id) REFERENCES capture_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_captured_images_session    ON captured_images (session_id);
CREATE INDEX IF NOT EXISTS idx_captured_images_material   ON captured_images (material_id, module);

-- Image annotations — operator labels per image per module.
-- Same image can have independent labels for tube vs stain (different features).
CREATE TABLE IF NOT EXISTS image_annotations (
    annotation_id TEXT    PRIMARY KEY,          -- UUID
    image_id      TEXT    NOT NULL,             -- FK → captured_images.image_id
    module        TEXT    NOT NULL,             -- which module this label applies to
    label         TEXT    NOT NULL,             -- 'good' | 'bad' | 'discard'
    annotated_at  TEXT    NOT NULL,
    annotated_by  TEXT,                         -- username from auth session
    FOREIGN KEY (image_id) REFERENCES captured_images(image_id)
);

CREATE INDEX IF NOT EXISTS idx_annotations_image_module ON image_annotations (image_id, module);
CREATE INDEX IF NOT EXISTS idx_annotations_module_label ON image_annotations (module, label);
-- Legacy: settings table kept for backward compatibility with existing databases.
-- shift_hours and all operator settings now live in config.json (v3.1.0+).
-- No code reads from this table — safe to drop in a future migration.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Authentication: users, sessions, activity log.
-- Session-based auth — no JWT.  Token is a random uuid4 stored server-side.
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    UNIQUE NOT NULL,
    password    TEXT    NOT NULL,                   -- bcrypt hash
    role        TEXT    NOT NULL DEFAULT 'operator', -- superAdmin | engineer | operator
    services    TEXT    NOT NULL DEFAULT '{}',       -- JSON: {"report":true,"master":false,...}
    email       TEXT,
    empid       TEXT,
    name        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,         -- 0=disabled, 1=active
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    -- v9: user-management contract fields (user-management-requirements.md)
    department  TEXT,
    shift       TEXT,
    require_password_change INTEGER NOT NULL DEFAULT 0,
    -- v9: profile fields (PUT /auth/me)
    phone         TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    country       TEXT,
    state         TEXT,
    district      TEXT,
    pincode       TEXT,
    -- v10: per-user header logo (Sieger-UI "Add Logo")
    logo_image    TEXT,                             -- base64-encoded image, no data: URI prefix
    logo_text     TEXT                               -- plain-text logo fallback
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users (username);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,                -- uuid4
    user_id     INTEGER NOT NULL REFERENCES users(id),
    username    TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    services    TEXT    NOT NULL,                   -- cached from users at login
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL,
    action      TEXT    NOT NULL,                   -- login | logout | start_inspection | teach | config_change | ...
    details     TEXT,                               -- JSON blob for action-specific context
    timestamp   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_log_username  ON activity_log (username);
CREATE INDEX IF NOT EXISTS idx_activity_log_timestamp ON activity_log (timestamp);
"""

_MIGRATION_V1_TO_V2 = """
-- Add teaching_sessions table (new in v2)
CREATE TABLE IF NOT EXISTS teaching_sessions (
    teaching_id   TEXT    PRIMARY KEY,
    module        TEXT    NOT NULL,
    scope_key     TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'training',
    created_at    TEXT    NOT NULL,
    completed_at  TEXT,
    n_samples     INTEGER,
    model_path    TEXT,
    threshold     REAL,
    extend_count  INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_teaching_module_scope  ON teaching_sessions (module, scope_key, status);
CREATE INDEX IF NOT EXISTS idx_teaching_status        ON teaching_sessions (status);

-- Add teaching_id columns to existing inspections table
ALTER TABLE inspections ADD COLUMN tube_teaching_id  TEXT;
ALTER TABLE inspections ADD COLUMN stain_teaching_id TEXT;
ALTER TABLE inspections ADD COLUMN uv_teaching_id    TEXT;
ALTER TABLE inspections ADD COLUMN tail_teaching_id  TEXT;
"""



_MIGRATION_V2_TO_V3 = """
-- Add capture sessions audit table (new in v3)
CREATE TABLE IF NOT EXISTS capture_sessions (
    session_id    TEXT    PRIMARY KEY,
    module        TEXT    NOT NULL,
    material_ids  TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    stopped_at    TEXT,
    images_saved  INTEGER NOT NULL DEFAULT 0,
    stopped_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_capture_sessions_module     ON capture_sessions (module);
CREATE INDEX IF NOT EXISTS idx_capture_sessions_started_at ON capture_sessions (started_at);

-- Add captured images table (new in v3)
CREATE TABLE IF NOT EXISTS captured_images (
    image_id      TEXT    PRIMARY KEY,
    session_id    TEXT    NOT NULL,
    material_id   TEXT    NOT NULL,
    module        TEXT    NOT NULL,
    captured_at   TEXT    NOT NULL,
    vl_path       TEXT,
    uv_path       TEXT,
    tail_path     TEXT,
    FOREIGN KEY (session_id) REFERENCES capture_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_captured_images_session    ON captured_images (session_id);
CREATE INDEX IF NOT EXISTS idx_captured_images_material   ON captured_images (material_id, module);

-- Add image annotations table (new in v3)
CREATE TABLE IF NOT EXISTS image_annotations (
    annotation_id TEXT    PRIMARY KEY,
    image_id      TEXT    NOT NULL,
    module        TEXT    NOT NULL,
    label         TEXT    NOT NULL,
    annotated_at  TEXT    NOT NULL,
    annotated_by  TEXT,
    FOREIGN KEY (image_id) REFERENCES captured_images(image_id)
);

CREATE INDEX IF NOT EXISTS idx_annotations_image_module ON image_annotations (image_id, module);
CREATE INDEX IF NOT EXISTS idx_annotations_module_label ON image_annotations (module, label);
"""


_MIGRATION_V3_TO_V4 = """
-- Add validation_json column to teaching_sessions (new in v4)
ALTER TABLE teaching_sessions ADD COLUMN validation_json TEXT;
"""

_MIGRATION_V4_TO_V5 = (
    "CREATE TABLE IF NOT EXISTS settings (\n"
    "    key         TEXT PRIMARY KEY,\n"
    "    value       TEXT NOT NULL,\n"
    "    updated_at  TEXT NOT NULL\n"
");\n"
"INSERT OR IGNORE INTO settings (key, value, updated_at)\n"
"VALUES ('shift_hours', '8.0', datetime('now'));\n"
)

_MIGRATION_V5_TO_V6 = """
-- Auth tables (new in v6)
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    UNIQUE NOT NULL,
    password    TEXT    NOT NULL,
    role        TEXT    NOT NULL DEFAULT 'operator',
    services    TEXT    NOT NULL DEFAULT '{}',
    email       TEXT,
    empid       TEXT,
    name        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users (username);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    username    TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    services    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    details     TEXT,
    timestamp   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_log_username  ON activity_log (username);
CREATE INDEX IF NOT EXISTS idx_activity_log_timestamp ON activity_log (timestamp);
"""

_MIGRATION_V6_TO_V7 = """
-- Manifest-driven teach capture workflow (v7)
-- Add operator_id, capture_status to capture_sessions
ALTER TABLE capture_sessions ADD COLUMN operator_id     TEXT;
ALTER TABLE capture_sessions ADD COLUMN capture_status  TEXT NOT NULL DEFAULT 'OPEN';
"""

_MIGRATION_V7_TO_V8 = """
-- Audit gallery: UV/Tail images alongside VL (v8)
ALTER TABLE inspections ADD COLUMN audit_image_uv    TEXT;
ALTER TABLE inspections ADD COLUMN audit_image_tail  TEXT;
"""

_MIGRATION_V8_TO_V9 = """
-- User-management contract fields + profile support (v9)
-- See docs/criticalfix/user_management_api_contract_compliance.md
ALTER TABLE users ADD COLUMN department  TEXT;
ALTER TABLE users ADD COLUMN shift       TEXT;
ALTER TABLE users ADD COLUMN require_password_change INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN phone         TEXT;
ALTER TABLE users ADD COLUMN address_line1 TEXT;
ALTER TABLE users ADD COLUMN address_line2 TEXT;
ALTER TABLE users ADD COLUMN country       TEXT;
ALTER TABLE users ADD COLUMN state         TEXT;
ALTER TABLE users ADD COLUMN district      TEXT;
ALTER TABLE users ADD COLUMN pincode       TEXT;
"""

_MIGRATION_V9_TO_V10 = """
-- Per-user header logo (v10) — Sieger-UI "Add Logo" (Header.jsx)
ALTER TABLE users ADD COLUMN logo_image TEXT;
ALTER TABLE users ADD COLUMN logo_text  TEXT;
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database and apply schema migrations.

    Args:
        db_path: Absolute path to sieger.db — typically sieger_data/sieger.db.

    Returns:
        Open sqlite3.Connection with WAL mode and foreign keys enabled.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")    # allows concurrent reads during writes
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, faster than FULL

    # Apply full DDL (no-op if tables already exist)
    conn.executescript(_DDL)

    # Read current schema version
    cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()

    if row is None:
        # Fresh database
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        logger.info("SQLite db initialised at %s (schema v%d)", db_path, SCHEMA_VERSION)

    elif row[0] < SCHEMA_VERSION:
        # Migrate
        current = row[0]
        logger.info("Migrating SQLite schema v%d → v%d at %s", current, SCHEMA_VERSION, db_path)

        if current == 1:
            conn.executescript(_MIGRATION_V1_TO_V2)
            current = 2

        if current == 2:
            conn.executescript(_MIGRATION_V2_TO_V3)
            current = 3

        if current == 3:
            conn.executescript(_MIGRATION_V3_TO_V4)

        if current == 4:
            conn.executescript(_MIGRATION_V4_TO_V5)
            current = 5

        if current == 5:
            conn.executescript(_MIGRATION_V5_TO_V6)
            current = 6

        if current == 6:
            conn.executescript(_MIGRATION_V6_TO_V7)
            current = 7

        if current == 7:
            conn.executescript(_MIGRATION_V7_TO_V8)
            current = 8

        if current == 8:
            conn.executescript(_MIGRATION_V8_TO_V9)
            current = 9

        if current == 9:
            conn.executescript(_MIGRATION_V9_TO_V10)
            current = 10

        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        conn.commit()
        logger.info("Migration complete — schema now v%d", SCHEMA_VERSION)

    else:
        logger.info("SQLite db opened at %s (schema v%d)", db_path, row[0])

    return conn

