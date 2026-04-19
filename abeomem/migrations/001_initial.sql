-- abeomem initial schema (design.md §1.2.2, v2.4.2 with CHECK constraints from fix #6).

CREATE TABLE memo (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  scope            TEXT    NOT NULL CHECK (
                              scope = 'global'
                              OR scope GLOB 'repo:[0-9a-f]*'
                              OR scope GLOB 'repo:path:[0-9a-f]*'
                           ),
  kind             TEXT    NOT NULL CHECK (
                              kind IN ('fix','gotcha','convention','decision')
                           ),
  title            TEXT    NOT NULL,
  symptom          TEXT,
  cause            TEXT,
  solution         TEXT,
  rule             TEXT,
  rationale        TEXT,
  notes            TEXT,
  tags             TEXT    NOT NULL DEFAULT '[]',
  topics           TEXT    NOT NULL DEFAULT '[]',
  superseded_by    INTEGER REFERENCES memo(id),
  archived_at      TEXT,
  useful_count     INTEGER NOT NULL DEFAULT 0,
  access_count     INTEGER NOT NULL DEFAULT 0,
  last_accessed_at TEXT,
  content_hash     BLOB    NOT NULL,
  created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (scope, content_hash)
);

CREATE INDEX memo_scope_kind ON memo (scope, kind);
CREATE INDEX memo_active
  ON memo (scope)
  WHERE superseded_by IS NULL AND archived_at IS NULL;

-- porter+unicode61 is English-optimised; non-English retrieval quality may be
-- lower. Acceptable for expected usage (programming memos, mostly English).
CREATE VIRTUAL TABLE memo_fts USING fts5(
  title, symptom, solution, rule, notes,
  content=memo, content_rowid=id,
  tokenize='porter unicode61'
);

CREATE TRIGGER memo_ai AFTER INSERT ON memo BEGIN
  INSERT INTO memo_fts (rowid, title, symptom, solution, rule, notes)
  VALUES (new.id, new.title, new.symptom, new.solution, new.rule, new.notes);
END;
CREATE TRIGGER memo_ad AFTER DELETE ON memo BEGIN
  INSERT INTO memo_fts (memo_fts, rowid, title, symptom, solution, rule, notes)
  VALUES ('delete', old.id, old.title, old.symptom, old.solution, old.rule, old.notes);
END;
CREATE TRIGGER memo_au AFTER UPDATE ON memo BEGIN
  INSERT INTO memo_fts (memo_fts, rowid, title, symptom, solution, rule, notes)
  VALUES ('delete', old.id, old.title, old.symptom, old.solution, old.rule, old.notes);
  INSERT INTO memo_fts (rowid, title, symptom, solution, rule, notes)
  VALUES (new.id, new.title, new.symptom, new.solution, new.rule, new.notes);
END;

CREATE TABLE memo_event (
  id         INTEGER PRIMARY KEY,
  ts         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  session_id TEXT    NOT NULL,
  action     TEXT    NOT NULL,
  memo_id    INTEGER,
  query      TEXT,
  topics     TEXT,
  payload    TEXT
);
CREATE INDEX memo_event_memo ON memo_event (memo_id, ts);
CREATE INDEX memo_event_ts   ON memo_event (ts);

-- Sentinel table for the .post.sql escape hatch (§1.2.3 fix #5).
-- Stage 1 ships zero .post.sql files; table is present so the runner can
-- track completion when one lands in Stage 3+.
CREATE TABLE migration_post_done (version INTEGER PRIMARY KEY);
