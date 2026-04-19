# abeomem — production-ready spec (v2.4.2)

MCP server that stores and retrieves tips, tricks, and bug fixes Claude Code already paid for once, so it doesn't pay again.

Supersedes v2.4.1. Applies 6 targeted pre-build clarifications identified by a second audit pass — no new features, no scope change. This is the spec to build from. Appendix C details the v2.4→v2.4.1 delta; Appendix D details v2.4.1→v2.4.2.

---

# Part I — What this is

## I.1 Target

**When CC is about to repeat a mistake, the relevant memo surfaces before the mistake happens.**

One sentence. Everything in Stage 1 exists to make this true on day one with a few dozen memos. Later stages make it stay true as the KB grows.

## I.2 Non-goals (permanent)

- Multi-user, auth, teams
- Vector search / embeddings (may appear in a late stage if BM25 fails; never primary)
- SessionStart auto-injection
- HTTP server, web UI
- Sync infrastructure (users put `~/.abeomem/` in git/Syncthing themselves if they want)
- A general-purpose note-taking tool
- Code-snippet storage (IDE snippets, gists — not this)

## I.3 Architecture

```
Claude Code ── stdio ──>  abeomem  ── SQLite (WAL, FTS5)
                            │        └── ~/.abeomem/memos/**/*.md  (mirror)
                            └── watchdog (500ms debounced)
```

Single Python 3.10+ process, stdio transport, local storage. SQLite is the source of truth; the markdown mirror is exported on write and reimported on change. Five MCP tools. Everything else is CLI.

## I.4 Staging

| Stage | Name | Gate | When |
|---|---|---|---|
| 1 | **Core** | ship | this weekend |
| 2 | Hardening | Stage 1 stable for 2 weeks | later |
| 3 | Evolution | `useful:retrieved > 0.1` sustained | when the metric says so |
| 4 | Scale | ≥1000 active memos OR search p95 >200ms | probably years |
| 5 | Polish | user demand | never, maybe |

Everything after Stage 1 is **listed, not specified**, in this doc. You'll spec it when the gate hits, not before.

---

# Stage 1 — Core

**Goal:** CC searches before it debugs, saves what it learns, and surfaces saved lessons across repos and sessions. Nothing more in Stage 1. Nothing less.

**Scope:**
- Five MCP tools: `memory_search`, `memory_get`, `memory_save`, `memory_update`, `memory_useful`
- Four kinds: `fix`, `gotcha`, `convention`, `decision`
- SQLite/WAL + FTS5 with sync triggers
- Migration runner
- Three-rule scope resolution
- Content hashing (deterministic)
- CLAUDE.md trigger template
- Markdown mirror + watchdog reimport
- Auto-backup
- Event log (write-only for now; consumed in Stage 3)
- CLI: `init`, `serve`, `sync`, `backup`, `ls`, `show`, `edit`, `chain`, `archive`, `topics`, `stats`, `scope`

**Explicitly out of scope for Stage 1:**
- Structured `refs` between memos — Stage 3
- Recall-time duplicate flagging — Stage 3
- `abeomem merge` — Stage 3
- Consolidation, review sessions, term reinforcement — Stage 3
- PreToolUse hook — Stage 3
- `abeomem prune`, `backup --verify` — Stage 2
- Structured JSONL logging — Stage 5

**Acceptance (must all hold):**
1. Two concurrent CC sessions in different repos save and search without contention errors.
2. DB remains consistent under crash at any point; migrations are all-or-nothing; backup + restore round-trips.
3. Identical-content re-save in the same scope returns `duplicate`, not a new row.
4. Two concurrent `memory_save(supersedes=X)` calls: exactly one wins; loser gets `superseded_target` with winner's id.
5. External `.md` edit is reflected in search within 2 seconds, debounced to one update event.
6. `abeomem stats` produces the full metrics table on day 1.

## 1.1 Install

```bash
uv tool install abeomem
abeomem init                                  # in a git repo: per-repo
abeomem init --global                         # once, globally
claude mcp add abeomem -- uvx abeomem serve
```

Deps: `fastmcp`, `rapidfuzz`, `watchdog`, `pyyaml`, `typer`. All pure-Python.

## 1.2 Data model

### 1.2.1 Bootstrap pragmas

On every connection open:

```sql
PRAGMA journal_mode = WAL;       -- concurrent read+write
PRAGMA synchronous  = NORMAL;    -- WAL-safe, 10× faster than FULL
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

**Multi-process access is expected and safe.** WAL mode supports multiple OS processes opening the same DB file concurrently — typical when running `abeomem serve` from several CC sessions (one per repo) at once. `busy_timeout` handles transient contention; readers never block writers and vice versa.

### 1.2.2 Schema (migration 001)

```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);  -- runner-owned

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
  tags             TEXT    NOT NULL DEFAULT '[]',   -- JSON; filter-only
  topics           TEXT    NOT NULL DEFAULT '[]',   -- JSON; affects ranking
  superseded_by    INTEGER REFERENCES memo(id),
  archived_at      TEXT,                             -- NULL = active
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

CREATE VIRTUAL TABLE memo_fts USING fts5(
  title, symptom, solution, rule, notes,
  content=memo, content_rowid=id,
  tokenize='porter unicode61'
  -- porter stemmer is English-optimized; non-English retrieval quality
  -- may be lower. Acceptable for the expected usage (programming memos,
  -- mostly English). Revisit if multilingual corpus grows.
);

-- FTS sync triggers (mandatory — external-content FTS does not auto-sync)
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
  action     TEXT    NOT NULL,  -- search | get | save | update | useful | archive
  memo_id    INTEGER,
  query      TEXT,
  topics     TEXT,
  payload    TEXT
);
CREATE INDEX memo_event_memo ON memo_event (memo_id, ts);
CREATE INDEX memo_event_ts   ON memo_event (ts);
```

### 1.2.3 Migration runner

On `abeomem serve` startup:

1. `INSERT OR IGNORE INTO schema_version VALUES (0)`.
2. Read current version. If `> MAX_KNOWN_VERSION`, abort: "downgrade not supported."
3. For each pending `N`: `BEGIN`, apply `migrations/NNN_<n>.sql`, `UPDATE schema_version SET version = N`, `COMMIT`.
4. On failure, rollback; abort startup with migration number + error.

Runner owns `schema_version`. Migration SQL files are DDL only — they never touch `schema_version`. Makes migrations replayable and avoids double-writes.

**File location.** Migrations ship as package data at `abeomem/migrations/*.sql`, discovered at runtime via `importlib.resources.files("abeomem.migrations")`. Naming: `NNN_<name>.sql` where `NNN` is zero-padded. Loaded in sorted order; the runner applies any with number > current version.

**Migration statement restrictions (enforced by runner).** Before applying migration `N`, the runner scans the SQL and rejects the file if it contains any of: `VACUUM`, `REINDEX`, `PRAGMA`, `ATTACH`, `DETACH`. These cannot run inside a transaction and would silently break the all-or-nothing guarantee (acceptance #2). A simple keyword scan on comment-stripped SQL is sufficient — migrations are author-controlled, not user input.

**Non-transactional escape hatch (`.post.sql`).** If a future migration genuinely needs a non-transactional step, split it: a transactional `NNN_<name>.sql` (DDL that bumps schema_version) plus an idempotent companion `NNN_<name>.post.sql` that runs **after** the schema_version commit. A small `migration_post_done (version INTEGER PRIMARY KEY)` table records completion so crashes between the two halves recover on next startup: the runner re-runs any `.post.sql` whose version ≤ schema_version but isn't in `migration_post_done`, then inserts the sentinel.

Stage 1 ships zero `.post.sql` files — the mechanism is specified so Stage 3+ doesn't have to retrofit it. The `migration_post_done` table is created by migration 001 alongside `schema_version`.

### 1.2.4 `content_hash`

```python
def content_hash(m) -> bytes:
    import unicodedata, hashlib
    n = lambda s: unicodedata.normalize("NFC", s or "")
    topics = sorted(unicodedata.normalize("NFC", t) for t in m.topics)
    tags   = sorted(unicodedata.normalize("NFC", t) for t in m.tags)
    parts = [n(m.kind), n(m.title),
             n(m.symptom), n(m.cause), n(m.solution),
             n(m.rule), n(m.rationale), n(m.notes),
             "|".join(topics), "|".join(tags)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
```

NFC before sort on topics/tags: `café` (NFC) and `café` (NFD) sort identically but hash differently without this. `\x1f` prevents field-boundary collisions.

### 1.2.5 Scope

Three forms, three rules. No project-marker heuristics, no `.abeomem-scope` file — deliberately minimal.

```
repo:<sha256(normalized_remote)[:16]>    # git with remote
repo:path:<sha256(toplevel_path)[:16]>   # git without remote
global                                    # everything else
```

**Resolution** (first match wins):

1. `git rev-parse --show-toplevel` succeeds AND `git remote get-url origin` succeeds → `repo:<hash>` using the normalized remote. Anchor = the toplevel directory.
2. `git rev-parse --show-toplevel` succeeds, no remote → `repo:path:<hash>` using the toplevel. Anchor = toplevel.
3. Else → `global`. Anchor = CWD.

Only three rules, no ambiguity. Non-git project dirs get `global` — if you want them scoped separately, run `git init`.

**Override:** `abeomem serve --scope <scope-id>`.

**Remote URL normalization** (non-negotiable — unnormalized hashes diverge):

1. `git@host:path` → `https://host/path`
2. Lowercase
3. Strip trailing `.git`
4. Strip trailing `/`
5. `://www.` → `://`

All of these normalize to one hash: `git@github.com:Abeo/project.git`, `https://github.com/abeo/project.git`, `https://github.com/abeo/project/`, `https://www.github.com/Abeo/project`.

### 1.2.6 Kinds

Four kinds. Shape matters — different retrieval weight per kind.

| kind | required | optional | example |
|---|---|---|---|
| `fix` | symptom, cause, solution | notes | "TS build fails after pnpm update / `.pnpm` corrupted / `rm -rf node_modules && pnpm i`" |
| `gotcha` | symptom | cause, solution, notes | "`JSON.stringify` drops undefined values / by spec / use a replacer" |
| `convention` | rule | rationale, notes | "this repo uses pnpm workspaces — never npm" |
| `decision` | rule, rationale | notes | "chose CockroachDB over Postgres / compliance requires multi-region" |

A `fix` is a bug you debugged (cause known, solution known). A `gotcha` is surprising behavior to remember. A `convention` is an unwritten rule. A `decision` is a choice worth revisiting later.

### 1.2.7 Event payload shapes

`memo_event.payload` is JSON. All writes go through one helper — `write_event(action, ...)` — which enforces these shapes. Never `INSERT INTO memo_event` directly elsewhere.

| action | payload |
|---|---|
| `search` | `{"k": int, "returned": int, "took_ms": int}` |
| `get` | `{"superseded": true}` if returning a superseded memo; `{"archived": true}` if returning an archived memo; else `NULL` |
| `save` | `{"status": "created" \| "duplicate", "supersedes": <id>?, "source": "tool" \| "watchdog"}` |
| `update` | `{"fields": [names], "source": "tool" \| "watchdog"}`, or `{"noop": true, "source": ...}` when identity check short-circuits |
| `useful` | `NULL` |
| `archive` | `{"reason": "string?", "source": "cli"}` |

Metrics (§1.9) read these literally. Add a field → update the metrics.

**`write_event()` contract.** On malformed payload (unknown action, wrong type, missing required field), raises `ValueError`. This is a programmer error, not a runtime condition — malformed payloads break metrics silently, so fail loudly. All call sites must catch (or let propagate) from inside tool handlers; `memory_*` tools translate to `internal_error` response.

### 1.2.8 Session and caller identity

Every `memo_event` row carries a `session_id` that identifies where the event came from:

- **MCP tools:** server mints a UUID4 when a stdio connection is accepted and attaches it to every event for that connection. One CC session = one stdio connection = one `session_id` string.
- **CLI commands that write events** (`abeomem archive`, `abeomem sync --import-new`, future CLI-driven writes): `session_id = "cli"` (literal string). Distinguishable from UUIDs at a glance.
- **Watchdog-triggered writes** (external `.md` edit reimported via `memory_update`, payload `source: "watchdog"`): `session_id = "watchdog"` (literal string). Parallel to `"cli"` — a named non-MCP write source.
- **Startup reconciliation and internal repair** (re-exporting a missing `.md` from DB; fixing out-of-sync files): do NOT write events. These are not user actions; they're consistency maintenance.

Metrics that measure "user MCP sessions" (e.g. `sessions_last_N_days` in Stage 2) filter `session_id NOT IN ('cli', 'watchdog')`. Metrics that measure "total user activity" may include both literals. Metrics that measure editor-driven curation specifically can filter `session_id = 'watchdog'`.

### 1.2.9 Topics vs tags

| | topics | tags |
|---|---|---|
| purpose | tech stack | free-form |
| affects ranking? | **yes** | no |
| vocabulary | lowercase, hyphen-separated, singular | whatever |
| examples | `python`, `nginx`, `ssl` | `urgent`, `flaky`, `postmortem-2026-04` |

**Normalization on save:** topics are lowercased, stripped, spaces → hyphens. `"Python"` becomes `python`. Tags stored verbatim.

Rule of thumb: if two memos share the term, should they rank higher for a query containing that term? Yes → topic. No → tag.

**Updating topics changes ranking immediately.** `memory_update(id, topics=[...])` re-indexes; that memo's position for topic-matched queries changes right away. If you want to preserve old ranking while adjusting for new context, use `memory_save(supersedes=...)` instead.

## 1.3 Tools

Five tools. All prefixed `memory_`. No archive tool — archive is CLI-only (§1.8).

### 1.3.1 Common error shape

```json
{"error": {"code": "<code>", "message": "<readable>", "details": {...}?}}
```

Codes:

| code | meaning | details |
|---|---|---|
| `not_found` | id doesn't exist | `{"id": <id>}` |
| `invalid_input` | validation failure | `{"field": "<n>", "reason": "..."}` |
| `superseded_target` | tried to supersede/update a non-tip | `{"tip_id": <id>}` |
| `internal_error` | unexpected | `NULL` |

**`duplicate` is not an error.** `memory_save` returns success with `{id, status: "duplicate"}` when dedup fires — the client sees a successful response with a status field, not an error. Duplicates are a valid outcome; the caller gets the existing id back.

No exceptions across the MCP boundary. Validation messages name the field: `"memory_save(kind='fix') missing required field: cause"`.

### 1.3.2 `memory_search`

> Search for lessons from prior sessions. Call this BEFORE debugging, BEFORE making non-obvious choices, and whenever you see an error you haven't seen this session.

**Input:**
```json
{
  "query":  "string",
  "kind":   "fix | gotcha | convention | decision | any",
  "scope":  "repo | global | both",
  "topics": ["string"]?,
  "k":      8
}
```

**Scope filter semantics:**
- `scope="global"` → filter `WHERE memo.scope = 'global'`
- `scope="repo"` → filter `WHERE memo.scope = <server's current scope>`. If the server started in `global` scope (not a git repo), this returns empty and the response includes `_warning: "server is in global scope; scope=repo returns empty"`.
- `scope="both"` → filter matches either the server's current scope or `global`. In `global` scope, `both` is equivalent to `global`.

**Response:**
```json
{
  "results": [{"id": 47, "kind": "fix", "title": "...", "snippet_line": "...", "score": 4.3}],
  "_hint": "After solving the problem, ask the user if a memo above helped. If yes, call memory_useful(id)."
}
```

`_hint` appears when `results` is non-empty AND no `useful` event has fired in the current session. Prevents habituation. "Current session" = current stdio MCP connection, identified by `session_id` (§1.2.8). A reconnecting CC gets a new session and may see `_hint` again.

Superseded and archived memos never appear. Audit via `abeomem chain <id>` or `abeomem ls --include-archived`.

### 1.3.3 `memory_get`

> Fetch full memo by id. Bumps `access_count` and updates `last_accessed_at`.

**Input:** `{id: int}`.

**Response:** all content fields plus:
```json
"superseded_by": 89,    // null if latest
"supersedes": 23,       // null if no ancestor; computed at read
"archived_at": null     // timestamp if archived
```

`supersedes` is derived (`SELECT id FROM memo WHERE superseded_by = :id LIMIT 1`), not stored.

**Archived and superseded memos are returned normally** — fetching by explicit id works even if the memo isn't active. This lets CC follow a supersede chain backward or audit a reference from cached context. Search never returns these, so CC only reaches them via explicit `memory_get`.

Event payload:
- `{"superseded": true}` if the returned memo has `superseded_by IS NOT NULL`
- `{"archived": true}` if the returned memo has `archived_at IS NOT NULL`
- Otherwise `NULL`

Both flags signal "CC followed a stale id" — useful diagnostic for understanding how CC navigates the KB.

### 1.3.4 `memory_save`

> Save a hard-earned lesson. Save when a bug took >5 min to diagnose, you discovered a non-obvious project convention, or you made a decision you'd forget. Do NOT save secrets, scratchpad, anything already in repo docs, or the user's current thought.

**Input:**
```json
{
  "kind":       "fix | gotcha | convention | decision",
  "title":      "string (<16 words)",
  "symptom":    "string? (required: fix, gotcha)",
  "cause":      "string? (required: fix)",
  "solution":   "string? (required: fix)",
  "rule":       "string? (required: convention, decision)",
  "rationale":  "string? (required: decision)",
  "notes":      "string?",
  "tags":       ["string"],
  "topics":     ["string"],
  "supersedes": "int?"
}
```

**Scope:** always the server's current scope (§1.2.5). No `scope` parameter — promotion is a later-stage concern.

**Topic normalization:** applied before storage (§1.2.9).

**Dedup.** RapidFuzz `token_set_ratio ≥ 85` on `title + primary_field` (symptom for fix/gotcha; rule for convention/decision) against active memos in the same scope. On match: `{id: <existing>, status: "duplicate"}`. `access_count` is NOT bumped. Event logged.

Dedup is scope-local and best-effort. Concurrent saves in one session may both succeed. Cross-scope duplicates are accepted — curation handled in later stages.

**Supersede guard (CAS).** If `supersedes=X`:

1. Target must exist → else `not_found`.
2. Target must be active (not superseded, not archived) → else `superseded_target` with `details.tip_id`.
3. In one `BEGIN IMMEDIATE`:
   ```sql
   INSERT INTO memo (...) VALUES (...);     -- new_id
   UPDATE memo SET superseded_by = :new_id
     WHERE id = :supersedes AND superseded_by IS NULL;
   -- if changes() == 0: concurrent save won the race → ROLLBACK,
   -- return superseded_target with current tip.
   ```

CAS makes cycles and races structurally impossible.

**Write ordering:**
```
compute content_hash
BEGIN IMMEDIATE
  INSERT INTO memo (...)
  if supersedes:
    UPDATE with CAS; rollback if changes() != 1
COMMIT
write_event('save', ...)
export markdown file   -- best-effort; reconciliation fixes on next startup
```

**Returns:** `{id: int, status: "created" | "duplicate"}`.

### 1.3.5 `memory_update`

> Refine an existing memo: typo, clarification, extra notes, added tag. Preserves id, created_at, useful_count, access_count. If the memo's claim is now wrong, use `memory_save(supersedes=<id>)` instead.

**Input:**
```json
{
  "id": "int",
  "title": "string?", "symptom": "string?", "cause": "string?",
  "solution": "string?", "rule": "string?", "rationale": "string?",
  "notes": "string?",
  "append_notes": "string?",
  "tags": ["string"]?, "topics": ["string"]?
}
```

PATCH semantics:
- Fields **absent from the request JSON** are unchanged.
- Fields **present with value `null`** are treated as unchanged (equivalent to omission).
- Fields **present with value `""` (string) or `[]` (array)** are cleared.

This distinction matters because some JSON libraries serialize missing and null differently. The rule makes both mean the same thing — only explicit clearing values actually clear.

**`append_notes` concatenation rule:**
- If current `notes` is NULL or empty string → result `notes = append_notes` (no leading whitespace).
- Else → result `notes = current_notes.rstrip() + "\n\n" + append_notes.lstrip()`.

The `rstrip`/`lstrip` prevents accumulated blank lines across repeated appends. The separator is `\n\n` so appended text renders as a distinct markdown paragraph in the mirror. Deterministic concatenation means the same `append_notes` applied twice produces the same resulting `content_hash` on the second call — dedup via identity short-circuit catches redundant appends naturally.

**Validation:**
- At least one field besides `id` → else `invalid_input` with `details.reason: "no fields to update"`.
- Target must be **active** (not superseded AND not archived) → else `superseded_target` for superseded; `invalid_input` with `details.reason: "target is archived; unarchive via DB edit"` for archived.
- `append_notes` and `notes` are mutex → else `invalid_input`.
- **Post-merge kind validation:** after the patch, the merged row must still satisfy the kind's required-field contract. Emptying `cause` on a `fix` → `invalid_input` with `details.field: "cause"`.

**Single transaction + content_hash CAS (with bounded retry):**
```
# Attempt 1
BEGIN IMMEDIATE
  fetch current row (id, content_hash as old_hash)
  merge patch → new row
  compute new_hash
  if new_hash == old_hash:
    COMMIT
    write_event('update', {noop: true, source})
    return {id, updated_at: unchanged}
  UPDATE memo SET ..., content_hash = :new_hash, updated_at = CURRENT_TIMESTAMP
    WHERE id = :id AND content_hash = :old_hash
  if changes() == 1:
    COMMIT
    write_event('update', {fields: [...], source})
    rewrite markdown file (filename stable, §1.6)
    return {id, updated_at}
  ROLLBACK  -- lost the race; a concurrent writer committed between fetch and UPDATE

# Attempt 2 (exactly one retry)
BEGIN IMMEDIATE
  re-fetch current row (id, content_hash as old_hash_2)
  re-merge patch onto the *new* current state
  recompute new_hash_2
  if new_hash_2 == old_hash_2:
    COMMIT
    write_event('update', {noop: true, source})  -- concurrent writer already applied it
    return {id, updated_at: unchanged}
  UPDATE memo SET ..., content_hash = :new_hash_2, updated_at = CURRENT_TIMESTAMP
    WHERE id = :id AND content_hash = :old_hash_2
  if changes() == 1:
    COMMIT
    write_event('update', {fields: [...], source})
    rewrite markdown file
    return {id, updated_at}
  ROLLBACK
  return internal_error  -- two consecutive races means sustained contention; diagnostic only
```

Fetch-through-UPDATE in one transaction closes the lost-update race. The retry re-merges the patch onto fresh state — never the same `old_hash` twice — so PATCH semantics (preserves id, created_at, useful_count, access_count) hold even when a concurrent writer mutates the row between our fetch and UPDATE. Two consecutive losses is impossible in a single-user tool; `internal_error` at that point is a diagnostic, not an expected branch.

**Returns:** `{id, updated_at}`.

**Update vs supersede:**

| Change | Mechanism |
|---|---|
| Typo, clarification, added tag, extra notes | `memory_update` |
| Missed a cause or edge case | `memory_update` |
| Project migrated npm → pnpm; old memo now wrong | `memory_save(supersedes=<old>)` |
| Decision reversed; preserve audit trail | `memory_save(supersedes=<old>)` |

### 1.3.6 `memory_useful`

> Call this ONLY after the user has explicitly confirmed that a retrieved memo helped. Ask first: "Did memo #<id> help?" — if yes, call. Do NOT call based on your own judgment.

**Input:** `{id: int}`.
**Returns:** `{useful_count: int}`.

**Design intent.** The user is the rater; CC is the messenger. Self-rating contaminates the signal (CC is optimistic about its own reasoning). Lower volume with honest signal > high volume of CC-graded noise.

Allowed on superseded memos (records on that id, not propagated). Search filters superseded by default, so it doesn't affect retrieval.

Load-bearing for later stages (reinforcement learning at Stage 3 depends on it). Reinforced in three places:
1. CLAUDE.md template (§1.7).
2. Conditional `_hint` in search responses (§1.3.2).
3. Metrics (§1.9) surface under-calling.

## 1.4 Retrieval ranking

```
candidates:
  superseded_by IS NULL AND archived_at IS NULL
  × scope filter
  × kind filter

score:
  FTS5 BM25 on (title ×3, symptom ×2, solution ×2, rule ×2, notes ×1)
  × (1 + log(1 + useful_count))                  # feedback boost
  × (1 + 0.5 × topic_overlap)  if topics passed   # context boost

topic_overlap = |query_topics ∩ memo_topics| / |query_topics|

→ top-k
```

Asymmetric denominator on purpose: a memo with topics `[nginx, ssl, http2]` gets full boost for a query with `topics=[ssl]`. Extra memo topics don't penalize. Preserves cross-cutting recall (ssl-in-nginx-or-python). Future readers: don't "fix" this to symmetric Jaccard.

BM25 on short structured fields + topic re-rank is fast enough for KBs up to ~10,000 memos on a laptop. Revisit at Stage 4 gate.

## 1.5 Topic vocabulary

No enforced taxonomy. Conventions:

- **Languages**: `python`, `typescript`, `javascript`, `rust`, `go`, `c++`, `java`, `bash`
- **Runtime**: `nodejs`, `deno`, `nextjs`, `fastapi`, `django`, `react`
- **Infra**: `nginx`, `ssl`, `tls`, `docker`, `kubernetes`, `postgres`, `redis`, `s3`
- **Concerns**: `auth`, `perf`, `memory-leak`, `concurrency`, `regex`

CC should pass topics when context is obvious. Multi-topic is common. For cross-cutting concerns pass the protocol without the language (`[ssl]`).

## 1.6 Markdown mirror

**Export on write.** Every `memory_save` and `memory_update` writes `~/.abeomem/memos/<scope_dir>/<kind>/<id>-<slug>.md`.

**Atomic write:** export writes to a temp file in the same directory (e.g., `<id>-<slug>.md.tmp-<pid>`), `fsync`s, then `os.replace()` to the final name. POSIX guarantees atomic rename within the same filesystem. A SIGKILL mid-write leaves at most a `.tmp-*` file that's cleaned up on next startup reconciliation.

**Export failure is non-fatal to the tool call.** If disk is full, permission denied, etc., the DB commit has already succeeded — we return success to MCP and log a WARNING to stderr: `"export failed for memo <id>: <error>; startup reconciliation will re-export."` The DB is source of truth; mirror is eventually consistent.

**Scope directory naming.** Colons break on Windows and some sync services. DB uses `repo:`, filesystem uses `repo-`:

```
DB scope         →  filesystem dir
global           →  memos/global/
repo:a1b2…       →  memos/repo-a1b2…/
repo:path:c3d4…  →  memos/repo-path-c3d4…/
```

**Filename stability.** Slug generated once on first export; never renamed, even when title changes. Preserves git history on users' memo dirs.

**Slug algorithm** (deterministic):

```python
import re, unicodedata
def slugify(title: str) -> str:
    # NFKD + ASCII strip for consistent cross-platform behavior
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)    # drop non-alphanumeric
    s = re.sub(r"[\s_-]+", "-", s)         # collapse whitespace/underscores to hyphen
    s = s.strip("-")[:60]                  # trim and cap length
    return s or "untitled"                 # never empty
```

Run once on first export; result lives in the filename on disk and in `memo_event` payload for the creating `save`. Nothing else stores the slug.

**Frontmatter + body (active memo):**
```markdown
---
id: 47
scope: repo:a1b2c3d4e5f6g7h8
kind: fix
topics: [python, asyncio]
tags: []
useful_count: 3
created: 2026-04-18T10:30:00Z
updated: 2026-04-18T14:22:00Z
---

# asyncio CancelledError silently swallowed

**Symptom:** …
**Cause:** …
**Solution:** …

## Notes
…
```

**Archived memo variant.** Archived memos stay on disk with extra frontmatter fields and a banner in the body:

```markdown
---
id: 47
scope: repo:a1b2c3d4e5f6g7h8
kind: fix
archived_at: 2026-04-18T10:30:00Z
archived_reason: "duplicate of #89"
topics: [python, asyncio]
...
---

> **⚠ This memo is archived.** Search never returns it. Kept on disk for audit and potential undelete via DB edit.

# asyncio CancelledError silently swallowed
...
```

Users manually browsing via Obsidian see the banner immediately. `abeomem archive <id>` triggers a re-export with banner. Unarchive (via DB edit of `archived_at = NULL`) re-exports without it on next reconciliation.

**Watchdog reimport** (debounced 500ms per path):

| Event | Behavior |
|---|---|
| Modified `.md` | Parse frontmatter + body → `memory_update`, source=`watchdog` |
| New `.md` no DB row | Log warning, skip. `abeomem sync --import-new` to opt in |
| Deleted `.md` | Log warning, leave DB unchanged. Use `abeomem archive` for removal |
| Malformed frontmatter | Skip with warning |

Debouncing is mandatory — editor write-rename-write sequences produce event storms. One save, one update event.

**Filename id authoritative.** If frontmatter `id:` differs from the filename, filename wins. Prevents fat-finger corruption of the wrong memo.

**Mirror consistency (honest):** the DB is atomic (ACID, WAL). The mirror is *eventually consistent* — a file write after COMMIT can fail; a `SIGKILL` during export can leave a half-written file; an archive operation updates the DB in milliseconds but the mirror update is a best-effort file write afterward. The **startup reconciliation** catches all of these on next launch.

**Startup sequence (strict order):**

1. Open DB, apply bootstrap pragmas.
2. Run migrations.
3. Auto-backup if interval elapsed.
4. Reconciliation:
   - For each `.md`: if hash differs from DB, route through `memory_update`.
   - For each active memo with no matching `.md`: re-export from DB (no event; repair).
   - For each archived memo: if `.md` still shows active frontmatter, re-export with archived banner; else leave alone.
5. Start watchdog.
6. Start MCP listener.

Order matters. Watchdog before reconciliation would reimport reconciliation's own writes.

**`abeomem sync --import-new`** for orphan `.md` files: assigns a new id (even if filename suggests otherwise), renames in place to `<id>-<slug>.md`, logs `save` event with `source: "sync-import-new"`.

**Conflict rule.** Tool call + external edit race → tool call wins. In practice this doesn't happen; CC doesn't edit memos it didn't just retrieve.

## 1.7 Trigger strategy

Tools do nothing if CC doesn't call them at the right moment. Stage 1 has one trigger: CLAUDE.md.

**Template** (installed by `abeomem init`). Installation rules:

- `abeomem init` (no flags) in a git repo (scope rule 1 or 2) → writes to `<anchor>/CLAUDE.md` where anchor is the git toplevel.
- `abeomem init` (no flags) outside any git repo (scope rule 3, `global`) → refuses with: *"Not in a git repo. Use `abeomem init --global` for global install, or run `git init` first for per-project install."*
- `abeomem init --global` → writes to `~/.claude/CLAUDE.md`, regardless of CWD.

Idempotent via markers; reinstall replaces only content between markers:

```markdown
<!-- BEGIN abeomem -->
## Memory (abeomem)

Before non-trivial work, check prior lessons:
- Debugging an error: `memory_search(query=<symptom>, kind="fix", topics=<stack>)`
- Choosing an approach: `memory_search(query=<task>, kind="convention|decision", topics=<stack>)`
- When you see an error you haven't seen this session: always search
- Topics: pass what you're working on — `["python","asyncio"]`, `["nginx","ssl"]`. Reuse existing topics (`abeomem topics`) before inventing new ones.

After non-trivial work, record what you learned:
- Bug took >5 min to diagnose → `memory_save(kind="fix", topics=[...], ...)`
- Project rule not in docs → `memory_save(kind="convention", topics=[...], ...)`
- Architectural choice worth remembering → `memory_save(kind="decision", topics=[...], ...)`
- Always include topics.

When a retrieved memo needs refinement → `memory_update(id, ...)`.
When a retrieved memo is now wrong (project changed) → `memory_save(supersedes=<id>)`.
When a retrieved memo actually helped → ASK THE USER: "Did memo #<id> help?" If yes, call `memory_useful(id)`. Never call based on your own judgment — the user decides.
<!-- END abeomem -->
```

**Existing-file handling:** `abeomem init` only writes if (a) `CLAUDE.md` doesn't exist, (b) it contains existing abeomem markers, or (c) the user confirms appending. Never overwrites arbitrary content.

**Shared-repo warning.** If `CLAUDE.md` is tracked in git and no abeomem block exists: *"CLAUDE.md is tracked by git; installing here commits memory instructions to repo history. Continue? [y/N]"*.

**No SessionStart injection.** Recall is pull, not push.

## 1.8 CLI & config

### 1.8.1 Commands

```
abeomem init [--global]             setup; injects CLAUDE.md block with markers
abeomem serve [--verbose]           run MCP server (stdio); watchdog + reconciliation
abeomem sync [--import-new]         rescan memos dir; reimport changed files
abeomem backup [--out PATH]         checkpoint WAL + VACUUM INTO timestamped copy
abeomem ls [--kind K] [--topic T] [--tag T] [--scope S] [--limit N] [--json]
                                     scope defaults to current; --scope all returns all
abeomem show <id>                    print full memo as markdown to stdout
abeomem edit <id>                    open exported .md in $EDITOR (non-blocking)
abeomem chain <id>                   print supersede chain for id (root → tip)
abeomem archive <id> [--reason R]    soft-delete; excludes from search
abeomem topics [--min-count N]      list topics by frequency
abeomem stats [--json]               success metrics (30-day window)
abeomem scope [--show-remote]        print current-directory scope
```

**Output formats.** `ls`, `stats`, `topics`, `chain` default to a compact human-readable table (`rich.table.Table` rendered to the current terminal width). `--json` flag emits JSONL for scripting. `show` prints raw markdown to stdout. `scope` and `edit` print one line.

**Archive is CLI-only** in Stage 1 — human curation decision. No cascading anything in Stage 1 (no refs to cascade). If a memo has inbound references in a later stage, archive will warn; in Stage 1, just archives the row.

**Stdio discipline.** `abeomem serve` **must never write to stdout** — stdio MCP owns stdout; a stray byte corrupts JSON-RPC. `--verbose` goes to stderr. `2>/tmp/abeo.log` for debugging.

**`abeomem edit <id>`.** Opens in `$EDITOR`, exits immediately (non-blocking — avoids the vim-blocks-vscode-detaches dance). Reimport via watchdog (if `serve` running) or startup reconciliation. Errors if id doesn't exist.

**Auto-backup.** On `serve` startup, if the newest file in `[backup].dir` is older than `interval_days` (or missing), run the backup sequence before accepting tool calls. While `serve` runs, an in-process asyncio task sleeps `interval_days × 86400` seconds and then runs the backup sequence via `asyncio.to_thread` so the blocking SQLite calls don't starve the stdio reader. The backup uses a **dedicated** SQLite connection opened at backup-time and closed after — sharing the request-handler connection would deadlock because `wal_checkpoint(TRUNCATE)` blocks on active writers and any in-flight `BEGIN IMMEDIATE` on the same connection holds its own lock.

The in-process timer resets on every `serve` restart. CC sessions cycle often, so the timer rarely fires in practice — the **startup check is the real guarantor** of interval coverage. Stating this explicitly so future maintainers don't strengthen the timer at the cost of the startup path.

After any successful backup (startup or timer), if file count in `[backup].dir` exceeds `keep_count`, delete oldest-by-mtime until at `keep_count`. No compression, no tiered retention — just a rolling window.

**Backup sequence:**
```sql
PRAGMA wal_checkpoint(TRUNCATE);   -- fold WAL into main DB
VACUUM INTO '~/.abeomem/backups/kb-YYYYMMDD-HHMMSS.db';
```

The checkpoint is mandatory. `VACUUM INTO` without it silently omits uncommitted WAL pages — restore would lose recent writes.

### 1.8.2 Config

```toml
# ~/.config/abeomem/config.toml
[db]
path = "~/.abeomem/kb.db"

[memos]
dir         = "~/.abeomem/memos"
fsnotify    = true
debounce_ms = 500

[backup]
enabled       = true
dir           = "~/.abeomem/backups"
keep_count    = 8
interval_days = 7

[scope]
default_search = "both"      # "repo" | "global" | "both"

[retrieval]
default_k        = 8
dedup_threshold  = 85        # RapidFuzz token_set_ratio

[logging]
level = "info"               # "debug" | "info" | "warn"
```

## 1.9 Success metrics

`abeomem stats` — 30-day window fixed in Stage 1.

| Metric | Definition | Target | On miss |
|---|---|---|---|
| `retrievals_per_day` | `search` events ÷ days active | any non-zero | trigger broken (fix CLAUDE.md) |
| `useful:retrieved` | distinct `(session, memo)` pairs with `useful` ÷ with `get` | **>0.3** | trigger or content quality |
| `save_dedup_rate` | `save` events with `duplicate` ÷ all `save` | any | high = retrieval failing before save |
| `supersede_rate` | `save` events with `supersedes` ÷ all `save` | grows with age | static = stale accumulating |
| `topic_coverage` | active memos with ≥1 topic ÷ active memos | ~100% | CC skipping topics |
| `fsnotify_reimports_per_week` | non-noop `update` events with `source=watchdog` ÷ weeks | non-zero | user not editing |
| `backups_last_30_days` | files in backup dir newer than 30d | ≥4 | `[ALERT]` loud-red |

**Zero-denominator rule.** Metrics without denominator data render as `—` (em dash), never `0.0` or `NaN`.

**ANSI rendering.** Metrics below target prefix with `[ALERT]` and color red when stdout is a TTY. Plain prefix when piped.

**`useful:retrieved` realism.** Under user-confirmed policy, expect 0.15–0.30 when the system is working. Below 0.1 means CC isn't asking — fix the trigger, not storage.

## 1.10 Operational notes (README)

- `.db-wal` and `.db-shm` are part of the database. Never delete them.
- SIGKILL recovery is automatic on next open (WAL handles it).
- `~/.abeomem/memos/` is *exportable*, not canonical. Losing it is annoying; losing `kb.db` + backups is fatal.
- Downgrades are not supported — older abeomem refuses to open a DB at a higher schema version.

## 1.11 Implementation order

For the weekend build, work bottom-up:

1. Bootstrap: pragmas, `schema_version`, migration runner.
2. Schema + FTS triggers. Unit-test FTS sync.
3. Content hash: NFC-in-sort. Test `café` NFC vs NFD.
4. Scope resolution. Test all four remote-URL normalizations.
5. `write_event(action, ...)` helper.
6. Tools in order: `memory_save` (no supersede) → `memory_get` → `memory_search` (no topic boost). Verify via MCP client.
7. Extensions: supersede CAS, update identity + CAS, useful, topic boost, dedup.
8. Markdown mirror: export on save/update. Frontmatter round-trip test.
9. Watchdog + debouncer + startup reconciliation. Test with vim.
10. Backup + CLI subcommands. `init` last.
11. `abeomem stats`. Acceptance #6.

Any other order fights itself. Estimated: one focused weekend.

---

# Stage 2 — Hardening (later)

When: Stage 1 stable for 2 weeks.

Adds:
- `abeomem prune` (orphan/missing/out-of-sync md report)
- `abeomem backup --verify`
- Time-windowed stats (`--window 7d|30d|all`)
- CLAUDE.md gitignore detection
- Concurrency test suite (CI)
- Structured JSONL logging (optional)

No new MCP tools. No new concepts.

---

# Stage 3 — Evolution (when ratio earns it)

When: `useful:retrieved > 0.1` sustained for 2 weeks on real usage.

Adds:
- Structured references (`refs` column; `memory_save` + `memory_update` accept refs; `memory_get` returns dereferenced refs)
- `abeomem merge` (with `--dry-run`, cascades refs)
- `abeomem refs <id>` CLI
- Hard cap on reference fetching (tool description enforced, CLAUDE.md reminder)
- Recall-time `_likely_duplicates` flag in `memory_search`
- `manual_weight` column for rank demotion
- `memo_query_term` table + online reinforcement on `memory_useful`
- Consolidation job (weak, decay, save-after-miss, dedup sweep, co-retrieval, topic normalization)
- Review sessions: `abeomem review`
- PreToolUse + PostToolUse hook pair (opt-in)

This stage is specified *when built*, not before. Signal from Stage 1 dogfood determines scope.

---

# Stage 4 — Scale (if it ever comes)

When: ≥1000 active memos OR search p95 >200ms.

Levers (listed, not spec'd):
- Hybrid retrieval via RRF (BM25 + embeddings via local Ollama)
- Scope promotion surfaced in review
- Event log rotation (if rows >10M)

Don't pre-design. Gate-trigger is measurement, not schedule.

---

# Stage 5 — Polish (demand-only)

Build only when a user asks. Candidates:

- Editor UI (local web)
- Multi-machine sync (Syncthing/git/CRDT)
- Team sharing
- External embedding providers
- TUI for review
- Windows-native support

---

# Appendix A — Invariants (don't break at any stage)

- **User is the rater for `memory_useful`.** CC asks, user answers, CC relays.
- **Merge and archive are CLI, never MCP.** CC surfaces signals; humans decide.
- **Don't hard-delete.** Archive is reversible; delete isn't.
- **Don't inject memos at SessionStart.** Recall is pull.
- **Don't train a model on the log.** BM25 + counters + topic boost covers 95%.
- **Don't write to stdout from `abeomem serve`.** Stdio MCP owns it.
- **Don't optimize a metric that isn't `useful:retrieved`.** Everything else is secondary.
- **Don't add a feature to paper over a broken trigger.** If CC isn't searching, no search quality matters.
- **Don't ship a stage before its gate.** Speculative design ages badly.

---

# Appendix B — Open questions

Deferred until data demands an answer.

1. Dedup threshold (85). Revisit once 50+ memos exist.
2. `useful:retrieved` realistic floor under user-confirmed policy — measure.
3. Backup retention (8 × 7d). Monthly grandfathering after 2mo? Defer.
4. Language auto-tagging for `global` gotchas — probably yes in Stage 2.
5. Reinforcement weight and caps (Stage 3) — tune with A/B.

---

# Appendix C — Deltas from v2.4

v2.4 was focused and shippable but had 18 gaps that would have stalled an autonomous build. All 18 are resolved here. No new features, no scope change, no contradictions introduced.

## Blocking gaps resolved

| # | Gap | Fix (section) |
|---|---|---|
| 1 | `duplicate` listed as error code but `memory_save` returns it as a success status | Removed from error codes table; explicit note that `save` returns success with status (§1.3.1). |
| 2 | Session ID mint mechanism unspecified | New §1.2.8: server mints UUID4 per stdio connection; CLI writes use `"cli"` literal. |
| 3 | CLI-initiated events had no session_id convention | `session_id = "cli"` for CLI writes (§1.2.8). |
| 4 | `scope=repo` ambiguous when server is in `global` scope | Returns empty + `_warning`; `both` degenerates to `global` (§1.3.2). |
| 5 | `abeomem init` in `global` scope contradicted `--global` rules | Clarified: `init` without `--global` in a non-git dir refuses with clear message (§1.7). |
| 6 | `archived_at` behavior on `memory_get` and `memory_update` unspecified | `get` returns archived memos with `archived: true` event flag (§1.3.3). `update` refuses on archived targets with explicit error reason (§1.3.5). |
| 7 | Markdown export failure handling unspecified | Atomic temp-file + rename; failure is non-fatal to tool call; WARNING to stderr; startup reconciliation fixes on next run (§1.6). |
| 8 | Archived memo's `.md` file representation unspecified | Stays on disk with frontmatter `archived_at` + archived banner in body; never auto-deleted (§1.6). |

## Non-blocking gaps resolved

| # | Gap | Fix |
|---|---|---|
| 9 | Slug generation algorithm unspecified | 7-line deterministic function in §1.6: NFKD + ASCII strip + lowercase + non-alphanumeric removal + length cap. |
| 10 | FTS5 tokenizer's English bias undocumented | Note in §1.2.2 schema comment: porter+unicode61 is English-optimized, acceptable for expected usage. |
| 11 | `memory_update` null vs `""` semantics vague | Explicit: absent, `null` both = unchanged; `""` or `[]` = clear (§1.3.5). |
| 12 | Migration file location undefined | Package data at `abeomem/migrations/*.sql`, via `importlib.resources` (§1.2.3). |
| 13 | `_hint` "session" scope ambiguous | "Current session" = current stdio MCP connection by `session_id` (§1.3.2). |
| 14 | Multi-process WAL contract implicit | Note in §1.2.1: multi-process is expected and safe. |

## Polish resolved

| # | Gap | Fix |
|---|---|---|
| 15 | `abeomem show` output format | Raw markdown to stdout (§1.8.1). |
| 16 | `abeomem ls` output format | Default rich table; `--json` for scripting (§1.8.1). `stats` gets `--json` too. |
| 17 | `write_event()` payload validation policy | Raises `ValueError` on malformed payload; fail loudly so metrics don't silently break (§1.2.7). |
| 18 | Backup rotation policy | After each backup, delete oldest-by-mtime until at `keep_count` (§1.8.1). |

## Kept from v2.4 (unchanged)

- All correctness bedrock: WAL + pragmas, FTS triggers, CAS on supersede and update, NFC-in-sort content hash, migration-ownership split, post-merge kind validation, watchdog debouncing, strict startup order.
- All feature scope: five MCP tools, four kinds, scope resolution three rules, markdown mirror with reconciliation, auto-backup, metrics.
- All deferrals: `refs`, recall-time dedup, `merge`, review sessions, consolidation, term reinforcement — still Stage 3.
- All invariants in Appendix A.

## Line count

v2.4: 873 lines. v2.4.1: ~1000 lines. Growth is almost entirely clarifications of edge cases that would have stalled a build. No new features added.

## Build readiness

**All 18 gaps closed.** An implementer opening this spec cold can follow §1.11's 11-step implementation order and build to acceptance without asking questions. The correctness story is end-to-end specified: how data flows from tool call through transaction to mirror to reconciliation, and what happens on failure at each step.

Ready for build.

---

# Appendix D — Deltas from v2.4.1

Second-pass pre-build audit found 6 residual ambiguities that would each require the implementer to invent semantics mid-build. All 6 are resolved here. No new features, no scope change, no contradiction with v2.4.1 decisions.

## Correctness-adjacent

| # | Gap | Fix (section) |
|---|---|---|
| 1 | `memory_update` "retry once" on CAS failure didn't specify whether to re-submit the same UPDATE (broken) or re-fetch + re-merge + re-issue (correct) | Full pseudocode for both attempts in §1.3.5. Retry re-fetches, re-merges the patch onto fresh state, recomputes `new_hash`, guards with the fresh `old_hash`. Two consecutive losses → `internal_error` (diagnostic; impossible in a single-user tool). |
| 3 | `append_notes` concatenation separator unspecified — `\n\n` vs `\n` vs no separator each produce different `content_hash` values and different mirror rendering | §1.3.5 now states: `result = old.rstrip() + "\n\n" + new.lstrip()`, or `new` alone if prior notes empty/NULL. Deterministic → dedup via identity short-circuit catches redundant appends. |
| 5 | Migration runner trusts authors not to include non-transactional statements (`VACUUM`, `REINDEX`, `PRAGMA`, `ATTACH`, `DETACH`) that would silently break the "all-or-nothing" acceptance criterion | §1.2.3: runner scans and rejects files containing banned keywords. Specifies a `.post.sql` escape hatch for Stage 3+ needs (idempotent companion + `migration_post_done` sentinel table). Stage 1 ships zero `.post.sql` files; mechanism is spec-only. |

## Hygiene

| # | Gap | Fix (section) |
|---|---|---|
| 2 | §1.2.8 didn't name the `session_id` used by watchdog-triggered updates, so Stage 2 metrics couldn't cleanly distinguish "MCP user session" from "external file edit" | §1.2.8: added fourth bullet — watchdog writes use literal `"watchdog"`. Metrics filter `NOT IN ('cli', 'watchdog')` for MCP session counts, include both for total activity. |
| 4 | "Background timer fires every 7 days" left threading model ambiguous in an asyncio stdio server — threading.Timer would race on shared SQLite connection | §1.8.1: asyncio task + `asyncio.to_thread` + dedicated SQLite connection at backup-time. Explicitly states the startup check is the real guarantor; the in-process timer rarely fires because CC sessions cycle. |
| 6 | `kind` and `scope` were enforced in Python only — hand-edited DB, buggy future migration, or `--scope foo` override could insert rows no search filter would match (silent data loss) | §1.2.2: `CHECK` constraints on both. `kind IN (...)` enforces the four-kind enum as a DB invariant. `scope GLOB` pattern enforces the three-rule naming from §1.2.5 at insert time. |

## Not changed

- Architecture, tool count, schema shape, retrieval ranking, scope resolution, CLI surface, config surface — all identical to v2.4.1.
- Stage gates, non-goals, invariants (Appendix A), deferrals (Appendix B) — all identical.
- All 18 v2.4→v2.4.1 resolutions (Appendix C) — still stand.

## Net effect

~40 lines of spec growth across §1.2.2, §1.2.3, §1.2.8, §1.3.5, §1.8.1. Zero new tools, zero new concepts, zero new dependencies. Every edit closes a decision point the implementer would otherwise have had to make alone mid-build.

Ready for build.