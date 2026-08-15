-- droid-assistant schema (SRS §5.8).
--
-- Conventions:
--   * Absolute times are INTEGER milliseconds since the Unix epoch, UTC.
--     The viewer renders them in local time; the database never stores an offset.
--   * Utterance offsets (start_ms, end_ms) are relative to session start and are
--     therefore timezone-free.
--   * Structured columns hold JSON text. SQLite's json1 functions are used for
--     the handful of queries that need to look inside them.
--
-- Applied by store/migrations.py, which owns the version stamp in user_version.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    title               TEXT,
    state               TEXT NOT NULL DEFAULT 'recording',
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    mode                TEXT NOT NULL DEFAULT 'balanced',
    source_languages    TEXT NOT NULL DEFAULT '[]',   -- JSON array of BCP-47 codes
    target_language     TEXT NOT NULL DEFAULT 'en',
    vocabulary          TEXT NOT NULL DEFAULT '[]',   -- JSON array (FR-ASR-8)
    client_user_agent   TEXT,
    mic_label           TEXT,
    audio_constraints   TEXT NOT NULL DEFAULT '{}',   -- what the browser was asked for (FR-CAP-11)
    cloud_used          INTEGER NOT NULL DEFAULT 0,
    providers_used      TEXT NOT NULL DEFAULT '[]',
    cost_usd            REAL NOT NULL DEFAULT 0.0,    -- FR-CFG-7, estimated
    cost_breakdown      TEXT NOT NULL DEFAULT '{}',   -- JSON: per-component estimate
    local_only          INTEGER NOT NULL DEFAULT 0,   -- FR-CFG-6, per-session override
    audio_path          TEXT,
    audio_duration_ms   INTEGER,
    tags                TEXT NOT NULL DEFAULT '[]',
    participants        TEXT NOT NULL DEFAULT '[]',
    preset_id           TEXT,
    plugins             TEXT,                          -- JSON array; NULL ⇒ all enabled plugins
    metadata            TEXT NOT NULL DEFAULT '{}',
    dropped_chunks      INTEGER NOT NULL DEFAULT 0,   -- FR-SIG-1 / NFR-PERF-6
    created_at          INTEGER NOT NULL
);

-- Matches the history listing's ORDER BY exactly, including the `id` tiebreak
-- that keeps paging stable when two sessions share a millisecond. Ordering on
-- `started_at` alone left SQLite building a temp B-tree for the second term on
-- every page of the list.
--
-- A new *name* rather than a redefinition: `CREATE INDEX IF NOT EXISTS` sees
-- the old single-column index already there and does nothing, so an existing
-- database would have kept paying for that sort forever. The old one is
-- redundant once this exists — a `(started_at DESC)` lookup is served by this
-- index's prefix — so it is dropped.
DROP INDEX IF EXISTS idx_sessions_started;
CREATE INDEX IF NOT EXISTS idx_sessions_started_id ON sessions (started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_state      ON sessions (state);

CREATE TABLE IF NOT EXISTS speakers (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    label               TEXT NOT NULL,                -- "Speaker 1"
    idx                 INTEGER NOT NULL,             -- palette index (FR-UI-16)
    display_name        TEXT,                         -- rename target (FR-DIA-4)
    person_id           TEXT REFERENCES persons (id) ON DELETE SET NULL,
    centroid_embedding  BLOB,
    UNIQUE (session_id, idx)
);

CREATE TABLE IF NOT EXISTS utterances (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    start_ms            INTEGER NOT NULL,
    end_ms              INTEGER NOT NULL,
    speaker_id          TEXT REFERENCES speakers (id) ON DELETE SET NULL,
    language            TEXT,
    text                TEXT NOT NULL DEFAULT '',
    text_original       TEXT,                          -- pre-edit text (FR-SES-8)
    translation         TEXT,
    translation_state   TEXT NOT NULL DEFAULT 'none',  -- none|pending|done|failed|skipped
    confidence          REAL,
    words_json          TEXT,
    marked              INTEGER NOT NULL DEFAULT 0,    -- FR-CAP-18
    edited_at           INTEGER,
    embedding           BLOB,                          -- FR-DIA-5, stored from v1
    timings_json        TEXT,                          -- FR-SIG-4
    created_at          INTEGER NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_utterances_session ON utterances (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_utterances_time    ON utterances (session_id, start_ms);
CREATE INDEX IF NOT EXISTS idx_utterances_speaker ON utterances (speaker_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    plugin_name         TEXT NOT NULL,
    plugin_version      TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL,
    mime                TEXT NOT NULL DEFAULT 'text/markdown',
    content             TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,    -- FR-PLG-12
    superseded_by       TEXT REFERENCES artifacts (id) ON DELETE SET NULL,
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts (session_id, plugin_name, version DESC);

CREATE TABLE IF NOT EXISTS plugin_state (
    plugin_name         TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 1,
    config_json         TEXT NOT NULL DEFAULT '{}',
    last_error          TEXT,
    last_error_at       INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_tokens (
    token               TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    issued_at           INTEGER NOT NULL,
    expires_at          INTEGER NOT NULL,
    revoked_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tokens_session ON ingest_tokens (session_id);

-- Diagnostics and the replay buffer that lets a reconnecting viewer resume from
-- its last seq (SRS §5.4). Prunable.
CREATE TABLE IF NOT EXISTS events_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    type                TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    created_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events_log (session_id, seq);

-- FR-SES-14. A preset is the four or five choices that otherwise precede every
-- single recording.
CREATE TABLE IF NOT EXISTS presets (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    config_json         TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    last_used_at        INTEGER
);

-- v2 tables, created now so that embeddings recorded from v1 onward (FR-DIA-5)
-- have somewhere to attach when enrollment ships.
CREATE TABLE IF NOT EXISTS persons (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    created_at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS voiceprints (
    id                  TEXT PRIMARY KEY,
    person_id           TEXT NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
    embedding           BLOB NOT NULL,
    source_session_id   TEXT REFERENCES sessions (id) ON DELETE SET NULL,
    created_at          INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- Full-text search over transcripts *and* artifacts (FR-SES-10).
--
-- External-content tables: the FTS index stores no copy of the text, and the
-- triggers below keep it in step with the base tables. `unicode61` with
-- `remove_diacritics 2` is what makes a Latin-script Serbian query match text
-- written with diacritics.
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5 (
    text,
    translation,
    content = 'utterances',
    content_rowid = 'rowid',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS utterances_ai AFTER INSERT ON utterances BEGIN
    INSERT INTO utterances_fts (rowid, text, translation)
    VALUES (new.rowid, new.text, coalesce(new.translation, ''));
END;

CREATE TRIGGER IF NOT EXISTS utterances_ad AFTER DELETE ON utterances BEGIN
    INSERT INTO utterances_fts (utterances_fts, rowid, text, translation)
    VALUES ('delete', old.rowid, old.text, coalesce(old.translation, ''));
END;

CREATE TRIGGER IF NOT EXISTS utterances_au AFTER UPDATE ON utterances BEGIN
    INSERT INTO utterances_fts (utterances_fts, rowid, text, translation)
    VALUES ('delete', old.rowid, old.text, coalesce(old.translation, ''));
    INSERT INTO utterances_fts (rowid, text, translation)
    VALUES (new.rowid, new.text, coalesce(new.translation, ''));
END;

CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5 (
    content,
    content = 'artifacts',
    content_rowid = 'rowid',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS artifacts_ai AFTER INSERT ON artifacts BEGIN
    INSERT INTO artifacts_fts (rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_ad AFTER DELETE ON artifacts BEGIN
    INSERT INTO artifacts_fts (artifacts_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_au AFTER UPDATE ON artifacts BEGIN
    INSERT INTO artifacts_fts (artifacts_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO artifacts_fts (rowid, content) VALUES (new.rowid, new.content);
END;
