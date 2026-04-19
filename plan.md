# abeomem — build plan (v1.0)

Autonomous-agent coding plan derived from `design.md` v2.4.2. Tasks are sized for a single agent session (~1–4 hrs each). Each task has explicit dependencies, file scope, verification commands, and out-of-scope guards.

**Ground rules** (apply to every task — inherited from global CLAUDE.md):

- **Verify before claiming done.** Run the task's verification command; paste the output or a summary in the commit body. "Looks right" is not evidence.
- **Surgical changes.** Touch only files listed in the task's `Files` section. If you need another file, update the plan (add a task or extend the current one) rather than expanding scope silently.
- **Read before write.** If editing an existing file, `Read` it first. After editing, re-read before editing again.
- **One task per commit.** Commit message `<type>: <subject>` where subject starts with the task ID, e.g. `feat: T1.3 migration 001 schema + triggers`.
- **No skipped verification.** If a task's verification can't run (missing dep, env issue), mark the task `blocked` in the tracker and say why. Don't paper over.
- **Resuming a failed task.** Revert any partial work on the task's branch, re-read the task, restart. Don't "fix up" a half-done task without re-reading — you'll drift.

**Task tracker.** Keep this file updated:

- Unstarted: `[ ]`
- In progress: `[~]` (add `<!-- ACTIVE: <agent-name> <date> -->` on the line)
- Done: `[x]` (include commit SHA in parens after title)
- Blocked: `[!]` (add `<!-- BLOCKED: reason -->`)

**Whole-plan acceptance.** The plan is done when all 6 acceptance criteria from `design.md` §1 (acceptance) pass as automated tests in Phase 8. Not before.

---

## Repository layout (established by T0.1)

```
abeomem/
├── pyproject.toml
├── README.md
├── abeomem/                    # package
│   ├── __init__.py
│   ├── __main__.py             # python -m abeomem entrypoint
│   ├── cli.py                  # typer app root
│   ├── server.py               # fastmcp server + stdio bootstrap
│   ├── db.py                   # connection pool, pragmas, migration runner
│   ├── migrations/
│   │   └── 001_initial.sql
│   ├── models.py               # dataclasses for memo, event payloads
│   ├── hashing.py              # content_hash
│   ├── slug.py                 # slugify
│   ├── scope.py                # git probing + remote URL normalization
│   ├── topics.py               # normalization
│   ├── events.py               # write_event helper
│   ├── tools/                  # one file per MCP tool
│   │   ├── __init__.py
│   │   ├── save.py
│   │   ├── get.py
│   │   ├── search.py
│   │   ├── update.py
│   │   └── useful.py
│   ├── mirror/
│   │   ├── __init__.py
│   │   ├── export.py           # DB row → .md file (atomic)
│   │   ├── parse.py            # .md → update dict
│   │   ├── watcher.py          # watchdog + debouncer
│   │   └── reconcile.py        # startup reconciliation
│   ├── backup.py               # checkpoint + VACUUM INTO + rotation
│   ├── config.py               # TOML loader
│   └── claude_md.py            # CLAUDE.md installer
└── tests/
    ├── conftest.py
    ├── unit/                   # per-module tests
    └── acceptance/             # §1 acceptance suite
```

---

## Phase 0 — Scaffolding

### T0.1 — Project skeleton

- **Deps:** none
- **Files:** `pyproject.toml`, `abeomem/__init__.py`, `abeomem/__main__.py`, `tests/conftest.py`, `README.md`
- **Goal:** `uv pip install -e .` succeeds; `python -m abeomem --help` prints Typer usage (even if all commands are stubs); `pytest` runs (0 tests, 0 failures).
- **Impl notes:**
  - Python 3.10+ minimum. Pin in `pyproject.toml` `requires-python = ">=3.10"`.
  - Deps (design.md §1.1): `fastmcp`, `rapidfuzz`, `watchdog`, `pyyaml`, `typer`, `rich` (for CLI tables).
  - Dev deps: `pytest`, `pytest-asyncio`, `ruff`.
  - `__main__.py` just imports and calls `cli.app()` (which is a stub that prints "unimplemented" for now).
- **Verify:**
  ```bash
  uv pip install -e '.[dev]' && pytest && python -m abeomem --help
  ```
  Expect: install succeeds, pytest reports "no tests ran", help output lists the command names (stubs OK).
- **Out of scope:** any actual command implementation; CI config.

### T0.2 — Lint + format baseline

- **Deps:** T0.1
- **Files:** `pyproject.toml` (ruff config), `.pre-commit-config.yaml` (optional)
- **Goal:** `ruff check abeomem/` passes on the stub codebase. Establishes the baseline so later tasks can't introduce lint debt.
- **Impl notes:** use ruff defaults plus `I` (isort) and `UP` (pyupgrade). Don't enable `D` (docstrings) — the spec discourages unnecessary comments.
- **Verify:** `ruff check abeomem/ tests/` exits 0.
- **Out of scope:** type-checking (mypy/pyright) — defer unless a later task needs it.

---

## Phase 1 — Storage foundation (sequential; everything else depends on this)

### T1.1 — Connection helper + bootstrap pragmas

- **Deps:** T0.1
- **Files:** `abeomem/db.py`, `tests/unit/test_db.py`
- **Goal:** `get_connection(path)` returns a `sqlite3.Connection` with all four pragmas from design.md §1.2.1 applied. Supports opening the same file from multiple processes without errors.
- **Impl notes:**
  - Apply pragmas on **every** connection open — WAL is per-DB but the others are per-connection.
  - `sqlite3.connect(..., isolation_level=None)` so we control transactions explicitly.
  - Row factory: `sqlite3.Row`.
- **Verify:**
  ```bash
  pytest tests/unit/test_db.py -v
  ```
  Tests must include:
  1. `PRAGMA journal_mode` returns `wal` after open.
  2. Two connections to the same DB file can both read concurrently.
  3. `busy_timeout` query returns 5000.
- **Out of scope:** migration runner (T1.2); any table creation.

### T1.2 — Migration runner

- **Deps:** T1.1
- **Files:** `abeomem/db.py` (extend), `tests/unit/test_migrations.py`, fixtures `tests/fixtures/migrations_*/`
- **Goal:** `run_migrations(conn, migrations_dir)` implements design.md §1.2.3 exactly:
  1. Creates `schema_version` if missing (`INSERT OR IGNORE` at 0).
  2. Reads current version; aborts if > max known.
  3. For each pending file sorted by number: `BEGIN`, apply DDL, `UPDATE schema_version`, `COMMIT`. Rollback on failure.
  4. **Statement restrictions** (fix #5): scan each `.sql` file, reject if it contains `VACUUM`, `REINDEX`, `PRAGMA`, `ATTACH`, or `DETACH` outside comments.
  5. `.post.sql` mechanism: after the transactional `.sql` commits, run any matching `.post.sql` whose version ≤ current_version and is not in `migration_post_done`. Insert the sentinel on success.
- **Impl notes:**
  - Migrations dir is loaded via `importlib.resources.files("abeomem.migrations")` in production, but tests pass an explicit path.
  - Keyword scanner: strip `--` line comments and `/* */` block comments, then case-insensitive word-boundary match on banned keywords.
  - `migration_post_done` is a plain table created by migration 001 — the runner just reads/writes it.
- **Verify:**
  ```bash
  pytest tests/unit/test_migrations.py -v
  ```
  Required tests:
  1. Fresh DB: applies all fixture migrations; `schema_version` advances; idempotent on second run.
  2. Migration containing `VACUUM` → runner raises with clear error; DB unchanged.
  3. Migration containing `PRAGMA` in a comment → allowed.
  4. Crash simulation: raise mid-DDL → `schema_version` unchanged.
  5. `.post.sql` runs after its sibling; crash between the two → `.post.sql` re-runs on next startup; double-apply is idempotent thanks to sentinel.
- **Out of scope:** the actual migration 001 content.

### T1.3 — Migration 001: schema, indices, FTS, triggers, CHECK constraints

- **Deps:** T1.2
- **Files:** `abeomem/migrations/001_initial.sql`, `tests/unit/test_schema.py`
- **Goal:** Applying migration 001 yields a database exactly matching design.md §1.2.2 — including the CHECK constraints from fix #6 and the `migration_post_done` table from fix #5.
- **Impl notes:**
  - Copy §1.2.2 DDL verbatim, with CHECKs added to `memo`:
    ```sql
    scope TEXT NOT NULL CHECK (
      scope = 'global'
      OR scope GLOB 'repo:[0-9a-f]*'
      OR scope GLOB 'repo:path:[0-9a-f]*'
    ),
    kind TEXT NOT NULL CHECK (
      kind IN ('fix','gotcha','convention','decision')
    ),
    ```
  - Add `CREATE TABLE migration_post_done (version INTEGER PRIMARY KEY);`
  - **Do not** include any banned keyword (no `PRAGMA`, no `VACUUM`, etc.).
- **Verify:**
  ```bash
  pytest tests/unit/test_schema.py -v
  ```
  Required tests:
  1. After `run_migrations`: `memo`, `memo_fts`, `memo_event`, `schema_version`, `migration_post_done` all exist.
  2. `INSERT INTO memo (kind='note', ...)` → `sqlite3.IntegrityError`.
  3. `INSERT INTO memo (scope='foo', ...)` → `sqlite3.IntegrityError`.
  4. `INSERT INTO memo (scope='global', kind='fix', ...)` succeeds; `memo_fts` row appears via trigger.
  5. `UPDATE memo SET title = 'x' WHERE id = 1` updates `memo_fts` via trigger.
  6. `DELETE FROM memo WHERE id = 1` removes the `memo_fts` row.
- **Out of scope:** tool logic; any Python API over the schema.

---

## Phase 2 — Pure-logic utilities (parallelizable — no shared state)

Tasks T2.1 through T2.4 have no dependencies on each other. **Dispatch as parallel agents.**

### T2.1 — `content_hash`

- **Deps:** T0.1
- **Files:** `abeomem/hashing.py`, `tests/unit/test_hashing.py`
- **Goal:** `content_hash(memo)` matches design.md §1.2.4 byte-for-byte.
- **Impl notes:** 10-line function straight from the spec. `unicodedata.normalize("NFC", ...)` on all fields, sort NFC-normalized topics/tags, join with `"\x1f"`, sha256, return `.digest()` (bytes, not hex).
- **Verify:**
  ```bash
  pytest tests/unit/test_hashing.py -v
  ```
  Required tests:
  1. Identical inputs → identical hash.
  2. `café` (NFC) vs `café` (NFD) in topics → same hash (§1.2.4 key invariant).
  3. Field-boundary collision: `title="ab", symptom=""` vs `title="a", symptom="b"` → different hashes (the `\x1f` separator).
  4. Topics `["b","a"]` vs `["a","b"]` → same hash (sorted).
- **Out of scope:** integration with `memo` table.

### T2.2 — `slugify`

- **Deps:** T0.1
- **Files:** `abeomem/slug.py`, `tests/unit/test_slug.py`
- **Goal:** `slugify(title)` matches design.md §1.6 exactly.
- **Impl notes:** 7-line function from the spec. NFKD + ASCII strip, lowercase, drop non-alphanumeric, collapse whitespace/underscores to hyphens, trim, cap at 60, fallback to `"untitled"`.
- **Verify:**
  ```bash
  pytest tests/unit/test_slug.py -v
  ```
  Required tests:
  1. `"TS build fails after pnpm update"` → `"ts-build-fails-after-pnpm-update"`.
  2. `"café"` → `"cafe"`.
  3. `""` → `"untitled"`.
  4. `"!!!"` → `"untitled"`.
  5. 100-char title → result is ≤60 chars, no trailing hyphen.
- **Out of scope:** mirror file naming (Phase 5).

### T2.3 — Scope resolution

- **Deps:** T0.1
- **Files:** `abeomem/scope.py`, `tests/unit/test_scope.py`
- **Goal:** `resolve_scope(cwd) -> ScopeResult` implements design.md §1.2.5 three-rule resolution and §1.2.5 remote URL normalization.
- **Impl notes:**
  - `ScopeResult = namedtuple("ScopeResult", ["scope_id", "anchor_path"])`.
  - Use `subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], ...)` etc. Check `returncode == 0` and non-empty stdout.
  - Normalize remote URL per the 5-step rule. Write `normalize_remote_url(url)` as a separate function for testability.
  - Hash = `sha256(normalized_url.encode()).hexdigest()[:16]`.
  - The 16-char hash must be lowercase hex to satisfy migration 001's CHECK constraint.
- **Verify:**
  ```bash
  pytest tests/unit/test_scope.py -v
  ```
  Required tests:
  1. All five example URLs from §1.2.5 normalize to the same hash.
  2. Git repo with remote → `repo:<hash>`, anchor = toplevel.
  3. Git repo without remote (use `tmp_path` with `git init`) → `repo:path:<hash>`.
  4. Non-git dir → `("global", cwd)`.
  5. Hash is always 16 lowercase hex chars.
- **Out of scope:** `--scope` CLI override; scope used by tools.

### T2.4 — Topic normalization

- **Deps:** T0.1
- **Files:** `abeomem/topics.py`, `tests/unit/test_topics.py`
- **Goal:** `normalize_topic(t)` and `normalize_topics(list)` implement design.md §1.2.9 rules.
- **Impl notes:** lowercase, strip, spaces → hyphens. Idempotent. Deduplicate the list while preserving order.
- **Verify:**
  - `normalize_topic("Python")` → `"python"`
  - `normalize_topic("  memory leak  ")` → `"memory-leak"`
  - `normalize_topics(["Python", "python", "Go"])` → `["python", "go"]`
- **Out of scope:** tags (verbatim storage, no normalization needed).

---

## Phase 3 — Event plumbing

### T3.1 — `write_event` helper

- **Deps:** T1.3
- **Files:** `abeomem/events.py`, `abeomem/models.py` (event payload dataclasses), `tests/unit/test_events.py`
- **Goal:** Single entry point `write_event(conn, *, action, session_id, memo_id=None, query=None, topics=None, payload=None)` that validates the payload shape per design.md §1.2.7 and inserts into `memo_event`.
- **Impl notes:**
  - Six actions: `search`, `get`, `save`, `update`, `useful`, `archive`. Any other action → `ValueError`.
  - Per-action payload schema (fix #17, §1.2.7):
    - `search`: `{"k": int, "returned": int, "took_ms": int}` — all three required, all ints.
    - `get`: `None` OR `{"superseded": True}` OR `{"archived": True}`.
    - `save`: `{"status": "created"|"duplicate", "supersedes": int?, "source": "tool"|"watchdog"}`.
    - `update`: `{"fields": [str, ...], "source": "tool"|"watchdog"}` OR `{"noop": True, "source": ...}`.
    - `useful`: `None`.
    - `archive`: `{"reason": str?, "source": "cli"}`.
  - Validation failure raises `ValueError` with the offending field name. Tool handlers translate to `internal_error` at the MCP boundary.
  - `payload` and `topics` are JSON-serialized (`json.dumps`) before insert.
- **Verify:**
  ```bash
  pytest tests/unit/test_events.py -v
  ```
  Required tests:
  1. Each action with valid payload → row inserted; retrievable with correct JSON round-trip.
  2. Unknown action → `ValueError`.
  3. `save` missing `status` → `ValueError` naming the field.
  4. `search` with `k` as string → `ValueError`.
  5. `session_id` stored verbatim (tests cover UUID string, `"cli"`, `"watchdog"`).
- **Out of scope:** emitting events from tool handlers.

---

## Phase 4 — MCP tools (bottom-up per design.md §1.11)

Order is partly sequential: T4.1 → T4.2 → T4.3 must be serial because they establish the round-trip. After T4.3, T4.4–T4.8 are more independent but share `memory_save`.

### T4.1 — `memory_save` (minimal: no supersede, no dedup)

- **Deps:** T2.1, T2.3, T2.4, T3.1, T1.3
- **Files:** `abeomem/tools/save.py`, `tests/unit/test_tool_save.py`
- **Goal:** Save a memo, compute `content_hash`, insert row, emit `save` event. Implements design.md §1.3.4 **minus** supersede and dedup.
- **Impl notes:**
  - Kind validation (post-merge kind contract from §1.2.6): `fix` needs symptom+cause+solution; `gotcha` needs symptom; `convention` needs rule; `decision` needs rule+rationale.
  - Missing required field → return `invalid_input` error dict (don't raise across MCP boundary — §1.3.1).
  - Topic normalization via T2.4 before storage.
  - `scope` = server's current scope (passed in for testability; wired to T2.3 in server bootstrap later).
  - `BEGIN IMMEDIATE` around insert + event write.
  - Return `{"id": int, "status": "created"}`.
- **Verify:**
  ```bash
  pytest tests/unit/test_tool_save.py -v
  ```
  Required tests:
  1. Minimal valid `fix` → row + event inserted; returned id matches.
  2. `fix` missing `cause` → `{"error": {"code": "invalid_input", "details": {"field": "cause", ...}}}`.
  3. Topics `["Python", "AsyncIO"]` stored as `["python", "asyncio"]`.
  4. Concurrent inserts from two connections both succeed (no deadlock).
- **Out of scope:** supersede (T4.4), dedup (T4.5), markdown export (Phase 5).

### T4.2 — `memory_get`

- **Deps:** T4.1, T3.1
- **Files:** `abeomem/tools/get.py`, `tests/unit/test_tool_get.py`
- **Goal:** Implements design.md §1.3.3. Returns full memo; bumps `access_count` and `last_accessed_at`; derives `supersedes` at read time; emits `get` event with the right flags.
- **Impl notes:**
  - `supersedes` is derived: `SELECT id FROM memo WHERE superseded_by = :id LIMIT 1`.
  - Archived and superseded memos are returned normally (§1.3.3) — search filters them, `get` does not.
  - Event payload:
    - `{"superseded": True}` if `superseded_by IS NOT NULL`
    - `{"archived": True}` if `archived_at IS NOT NULL`
    - Can have both flags if both conditions hold — in that case prefer `{"superseded": True, "archived": True}`. (Clarify in test.)
    - Otherwise `None`.
  - Access bump and event write in the same `BEGIN IMMEDIATE` block.
- **Verify:**
  1. Active memo: full payload + correct access_count bump.
  2. Superseded memo returns with `superseded_by` populated; event payload `{"superseded": True}`.
  3. Archived memo returns with `archived_at` populated; event payload `{"archived": True}`.
  4. Non-existent id → `{"error": {"code": "not_found"}}`.
- **Out of scope:** search integration.

### T4.3 — `memory_search` (no topic boost, no `_hint`)

- **Deps:** T4.1, T3.1
- **Files:** `abeomem/tools/search.py`, `tests/unit/test_tool_search.py`
- **Goal:** Implements design.md §1.3.2 and §1.4 **minus** topic boost and `_hint`. BM25 with field weights + `useful_count` boost.
- **Impl notes:**
  - Candidate filter: `WHERE superseded_by IS NULL AND archived_at IS NULL`.
  - Scope filter per §1.3.2 (global | repo | both, with the global-scope-degenerate case).
  - FTS5 query: `SELECT memo.id, bm25(memo_fts, 3,2,2,2,1) AS rank FROM memo_fts JOIN memo ON memo.id = memo_fts.rowid WHERE memo_fts MATCH :q`.
  - BM25 in SQLite returns negative-correlated scores (lower = better). Convert to positive: `score = -bm25_rank × (1 + log(1 + useful_count))`. Document this in a comment.
  - Snippet: `snippet(memo_fts, 0, '', '', '…', 10)` on title column for `snippet_line`.
  - Query sanitization: escape `"` in user query by wrapping in `"..."` (phrase query). Avoid injecting FTS operators the user didn't intend.
  - `scope=repo` in global-scope server → return `{"results": [], "_warning": "server is in global scope; scope=repo returns empty"}`.
- **Verify:**
  1. Search for `"pnpm"` with a memo containing `pnpm` in title → hit with highest rank.
  2. `useful_count=5` memo ranks above `useful_count=0` memo with identical FTS score.
  3. Superseded memo never appears.
  4. Archived memo never appears.
  5. `scope=repo` in global scope → empty results + warning field.
  6. `k=3` returns at most 3 results.
- **Out of scope:** topic boost (T4.8), `_hint` emission (T4.8), dedup-at-recall.

### T4.4 — `memory_save` — supersede CAS

- **Deps:** T4.1
- **Files:** `abeomem/tools/save.py` (extend), `tests/unit/test_tool_save_supersede.py`
- **Goal:** Implements the supersede branch of design.md §1.3.4. CAS via `UPDATE ... WHERE id=:target AND superseded_by IS NULL`.
- **Impl notes:**
  - Pre-check: target exists (`not_found`) and is active (not superseded, not archived → `superseded_target` with `details.tip_id` = current tip).
  - Inside `BEGIN IMMEDIATE`: INSERT new row, then UPDATE with CAS. If `changes() == 0` → ROLLBACK, fetch current tip, return `superseded_target`.
  - "Current tip" = walk `superseded_by` chain until NULL.
- **Verify:**
  1. Normal supersede: returns `{"id": new_id, "status": "created", "supersedes": old_id}`; old row's `superseded_by = new_id`.
  2. Supersede non-existent id → `not_found`.
  3. Supersede already-superseded id → `superseded_target` with `details.tip_id` pointing to the tip.
  4. Supersede archived id → `superseded_target`.
  5. **Race test** (acceptance #4): two concurrent `memory_save(supersedes=X)` calls — exactly one wins, loser returns `superseded_target` with the winner's id. Use `threading.Barrier` to line them up.
- **Out of scope:** dedup (T4.5).

### T4.5 — `memory_save` — dedup via RapidFuzz

- **Deps:** T4.4
- **Files:** `abeomem/tools/save.py` (extend), `tests/unit/test_tool_save_dedup.py`
- **Goal:** Implements the dedup branch of design.md §1.3.4. RapidFuzz `token_set_ratio ≥ 85` on `title + primary_field` against active memos in the same scope.
- **Impl notes:**
  - Primary field: `symptom` for fix/gotcha; `rule` for convention/decision.
  - `active` = `superseded_by IS NULL AND archived_at IS NULL`.
  - Threshold from `[retrieval].dedup_threshold` config (default 85).
  - On match: return `{"id": existing_id, "status": "duplicate"}`. **Do not** bump `access_count`. Event payload `{"status": "duplicate", "source": "tool"}`.
  - Identical content_hash is a strict short-circuit (unique constraint): try insert first, catch `IntegrityError` on `(scope, content_hash)`, treat as duplicate with the pre-existing row's id.
- **Verify:**
  1. Identical save in same scope → `duplicate`, same id, no duplicate rows.
  2. 90%-similar title+symptom → `duplicate`.
  3. Same content in different scope → accepted as new.
  4. Dedup does **not** match superseded memos (they're not active).
  5. Save with `supersedes=X` skips dedup check (spec implies: supersede is an explicit override).
- **Out of scope:** cross-scope dedup (deferred per §1.3.4).

### T4.6 — `memory_update` (full: CAS retry + append_notes + kind revalidation)

- **Deps:** T4.1, T2.1
- **Files:** `abeomem/tools/update.py`, `tests/unit/test_tool_update.py`
- **Goal:** Implements design.md §1.3.5 **including fix #1 (two-attempt CAS) and fix #3 (`append_notes` concatenation)**.
- **Impl notes:**
  - Patch semantics: absent=unchanged, `null`=unchanged, `""`/`[]`=clear.
  - At least one field besides `id` required.
  - Target must be active; archived → `invalid_input` with reason; superseded → `superseded_target`.
  - `append_notes` + `notes` mutex.
  - **`append_notes` concat (fix #3):** `new = append_notes` if prior is empty/NULL, else `current.rstrip() + "\n\n" + append_notes.lstrip()`.
  - Post-merge kind validation: merged row must satisfy required-field contract for its kind.
  - Topic normalization on update.
  - **CAS retry (fix #1):** follow the two-attempt pseudocode in §1.3.5 byte-for-byte. On second loss → `internal_error`.
  - Noop path: if `new_hash == old_hash`, write `{"noop": true, "source": "tool"}` event and return unchanged `updated_at`.
  - Event fields list: only fields that actually changed (computed from pre vs post row).
- **Verify:**
  1. Simple field update → row updated, `updated_at` advances, event logged with `fields` list.
  2. `null` for a field → field unchanged.
  3. `""` for a field that's optional → field cleared.
  4. `""` for a required field given the kind → `invalid_input` naming the field.
  5. `append_notes` on empty notes → notes = append_notes, no leading whitespace.
  6. `append_notes` on existing notes → `old.rstrip() + "\n\n" + new.lstrip()`.
  7. No-op update (idempotent same payload) → returns unchanged `updated_at`; event has `noop: true`.
  8. Archived target → `invalid_input` with reason `"target is archived; unarchive via DB edit"`.
  9. Superseded target → `superseded_target`.
  10. **CAS race:** two concurrent updates with `threading.Barrier`; both succeed; final row reflects both field sets (retry re-merged).
- **Out of scope:** watchdog-sourced updates (Phase 5; will pass `source="watchdog"` instead of `"tool"`).

### T4.7 — `memory_useful`

- **Deps:** T4.1, T3.1
- **Files:** `abeomem/tools/useful.py`, `tests/unit/test_tool_useful.py`
- **Goal:** Implements design.md §1.3.6. Bumps `useful_count`, emits `useful` event (payload `None`).
- **Impl notes:** allowed on superseded memos (doesn't propagate). Not allowed on non-existent id → `not_found`. No validation beyond id existence.
- **Verify:**
  1. Increments `useful_count`, returns new value.
  2. Allowed on superseded memo.
  3. Non-existent id → `not_found`.
- **Out of scope:** reinforcement logic (Stage 3).

### T4.8 — `memory_search` — topic boost + `_hint`

- **Deps:** T4.3, T4.7
- **Files:** `abeomem/tools/search.py` (extend), `tests/unit/test_tool_search_topics.py`
- **Goal:** Implements full design.md §1.4 scoring and §1.3.2 `_hint` logic.
- **Impl notes:**
  - Topic boost: `score *= 1 + 0.5 × (|query_topics ∩ memo_topics| / |query_topics|)` when query topics are passed.
  - **Asymmetric denominator intentional** — add a code comment pointing to §1.4's "don't fix this to symmetric Jaccard" note.
  - `_hint` appears in response iff `results` is non-empty AND no `useful` event exists for the current `session_id`. Query: `SELECT 1 FROM memo_event WHERE session_id = :sid AND action = 'useful' LIMIT 1`.
  - Session_id plumbing: the tool needs access to the caller's session_id. Pass via a `ctx` object or function arg — establish the pattern here; server bootstrap wires it in Phase 6.
- **Verify:**
  1. Query with topics matching memo → score higher than same query without topics.
  2. Asymmetric: memo topics `[nginx, ssl, http2]` vs query topics `[ssl]` → full 0.5× boost (denominator is |query_topics| = 1).
  3. `_hint` appears on first search in a session with non-empty results.
  4. After `memory_useful` in same session → `_hint` absent.
  5. New session_id → `_hint` appears again even with the same query.
- **Out of scope:** duplicate flagging (Stage 3).

---

## Phase 5 — Mirror + watchdog

### T5.1 — Markdown export (atomic)

- **Deps:** T2.2, T4.1
- **Files:** `abeomem/mirror/export.py`, `tests/unit/test_mirror_export.py`
- **Goal:** `export_memo(memo_row, memos_dir)` writes `<memos_dir>/<scope_dir>/<kind>/<id>-<slug>.md` atomically with frontmatter per design.md §1.6 (including archived variant).
- **Impl notes:**
  - Scope dir translation: `repo:` → `repo-`, `repo:path:` → `repo-path-`.
  - Atomic write: temp file `<target>.tmp-<pid>` in same directory, `fsync`, `os.replace(tmp, target)`.
  - Filename stability: if file `<id>-*.md` already exists, use that slug (don't regenerate). If not, slug from current title.
  - Archived memo: add `archived_at` + `archived_reason` frontmatter fields, prepend banner `> **⚠ This memo is archived.** ...`.
  - Export failure non-fatal: catch `OSError`, log warning to stderr, return False. Caller does not abort.
- **Verify:**
  1. Active memo exports; file content matches expected frontmatter + body.
  2. Re-export after title change → filename unchanged, body updated.
  3. Archived memo exports with banner + extra frontmatter.
  4. Simulated `os.replace` failure → returns False, no partial file left.
  5. SIGKILL simulation (write temp file but don't replace): next call cleans up the `.tmp-*` file. (Actually this is reconciliation's job — test just verifies tmp file is created in same dir.)
- **Out of scope:** reconciliation; watchdog integration.

### T5.2 — Markdown parse

- **Deps:** T5.1
- **Files:** `abeomem/mirror/parse.py`, `tests/unit/test_mirror_parse.py`
- **Goal:** `parse_memo_file(path) -> dict` reads frontmatter + body, returns a dict suitable for `memory_update`.
- **Impl notes:**
  - Frontmatter: YAML between `---` fences. Use `pyyaml.safe_load`.
  - Body parser recognizes `**Symptom:** ...`, `**Cause:** ...`, `**Solution:** ...` inline fields and `## Notes` section.
  - **Filename id authoritative** (§1.6): extract id from filename regex `^(\d+)-.*\.md$`. If frontmatter id differs, filename wins (log warning).
  - Malformed frontmatter → return None (caller logs warning and skips).
  - Archived-banner body should round-trip: the banner is stripped from the parsed `body`, and `archived_at` is taken from frontmatter.
- **Verify:**
  1. Round-trip: export → parse → same fields.
  2. Edited title in body → parsed title reflects edit.
  3. Filename/frontmatter id mismatch → filename wins, warning logged.
  4. Missing `---` fences → returns None.
  5. Archived memo: `archived_at` present; body excludes banner.
- **Out of scope:** triggering update from parse (next task).

### T5.3 — Watchdog + debouncer (session_id = "watchdog")

- **Deps:** T5.2, T4.6
- **Files:** `abeomem/mirror/watcher.py`, `tests/unit/test_mirror_watcher.py`
- **Goal:** A `MemosWatcher` class starts a `watchdog.observers.Observer` on the memos dir, debounces events at 500ms per path, and routes:
  - Modified `.md` → parse → `memory_update(..., source="watchdog")` with `session_id="watchdog"` (fix #2).
  - New `.md` with no DB row → log warning, skip.
  - Deleted `.md` → log warning, skip.
  - Malformed frontmatter → log warning, skip.
- **Impl notes:**
  - Debouncer: simple `asyncio.Task` per path, cancelled + rescheduled on each event.
  - Use `watchdog.observers.Observer` in a thread; events cross into the asyncio loop via `loop.call_soon_threadsafe`.
  - Hash-before-update: compute `content_hash` from parsed fields; if matches DB row's current hash, skip (don't call `memory_update`, don't emit event). Filters out our own export-caused events.
- **Verify:**
  1. Modify a file externally → one `memory_update` call within 2s (acceptance #5).
  2. Editor write-rename-write storm (5 events in 100ms) → one update call after debounce.
  3. Orphan `.md` → skip + warning.
  4. Malformed `---` frontmatter → skip + warning.
  5. Self-triggered events (from our own export) → no update call.
  6. Event row written has `session_id = "watchdog"` and `source: "watchdog"` in payload.
- **Out of scope:** reconciliation.

### T5.4 — Startup reconciliation

- **Deps:** T5.1, T5.2, T5.3
- **Files:** `abeomem/mirror/reconcile.py`, `tests/unit/test_reconcile.py`
- **Goal:** Implements design.md §1.6 reconciliation step (called between backup and watchdog start).
- **Impl notes:**
  - For each `.md` in memos dir: parse, compute content_hash, compare to DB; if different, route through `memory_update` **with `session_id="watchdog"` and source=`"watchdog"`** (reconciliation treats external edits as watchdog-sourced for metric purposes).
  - For each active memo with no matching `.md`: re-export from DB, **no event** (repair, not user action).
  - For each archived memo with `.md` showing active frontmatter: re-export with banner.
  - Clean up leftover `.tmp-*` files from crashed previous runs.
- **Verify:**
  1. Modify a file while server is off → on startup, DB reflects edit.
  2. Delete a file while server is off → on startup, file is re-exported from DB.
  3. Leftover `<id>-<slug>.md.tmp-12345` → removed on startup.
  4. Archive a memo via direct DB edit → next startup re-exports with banner.
- **Out of scope:** the `sync --import-new` CLI subcommand (T6.2).

---

## Phase 6 — CLI + server bootstrap

### T6.1 — Config loader

- **Deps:** T0.1
- **Files:** `abeomem/config.py`, `tests/unit/test_config.py`
- **Goal:** Load `~/.config/abeomem/config.toml` into a typed config object with defaults from design.md §1.8.2.
- **Impl notes:** use `tomllib` (3.11+) or `tomli` backport. Expand `~` in all path fields. Missing file → defaults.
- **Verify:** defaults match §1.8.2; override via file works for each field; malformed TOML → clear error.
- **Out of scope:** writing config; validation beyond type.

### T6.2 — Server bootstrap + `abeomem serve`

- **Deps:** T4.1–T4.8, T5.3, T5.4, T6.1
- **Files:** `abeomem/server.py`, `abeomem/cli.py` (add `serve` command)
- **Goal:** `abeomem serve` brings up the full server per design.md §1.6 startup sequence — migrations, backup (placeholder from T6.4), reconciliation, watchdog, MCP listener.
- **Impl notes:**
  - Mint a fresh `session_id = str(uuid.uuid4())` when a stdio connection is accepted; thread it through tool handlers via `Context`.
  - **Stdio discipline (§1.8.1):** no `print()` to stdout anywhere in `serve`. All logging → stderr via `logging`. Add a test that runs `abeomem serve` in a subprocess and asserts stdout is pure JSON-RPC.
  - Tool registration: register all 5 MCP tools with FastMCP; each handler catches exceptions and returns `{"error": {"code": "internal_error", ...}}`.
  - Startup order strictly: pragmas → migrations → backup → reconciliation → watchdog → MCP listener. Fail loud on any step.
- **Verify:**
  1. Subprocess `abeomem serve` + JSON-RPC `tools/list` → returns five tool names.
  2. `memory_save` over MCP round-trips correctly.
  3. Stdout is pure JSON-RPC (no stray logs).
  4. Migration failure at startup → non-zero exit code, error on stderr.
- **Out of scope:** backup integration (T6.4) — serve uses a no-op backup placeholder until T6.4 lands.

### T6.3 — CLI commands (ls/show/edit/chain/archive/topics/sync/scope)

- **Deps:** T6.2
- **Files:** `abeomem/cli.py` (extend)
- **Goal:** Every command in design.md §1.8.1 implemented except `serve` (done), `backup` (T6.4), `init` (T6.5), `stats` (T7.1).
- **Impl notes:**
  - All write commands use `session_id="cli"`.
  - `archive <id> [--reason R]` → update `archived_at`, emit `archive` event, re-export `.md` with banner.
  - `edit <id>` → `subprocess.Popen([editor, path])` and return immediately. Don't wait.
  - `sync --import-new` → assign new id, rename in place, emit `save` event with `source: "sync-import-new"`.
  - `ls`, `topics`, `chain` → rich `Table` to stdout for TTY; JSONL for `--json`.
  - `show` → raw markdown of the exported file to stdout.
  - `scope [--show-remote]` → print current scope; with `--show-remote`, also print the normalized URL if applicable.
- **Verify:** one test per command, asserting correct stdout/DB state for a minimal case.
- **Out of scope:** see task list.

### T6.4 — Backup (sequence + asyncio task, fix #4)

- **Deps:** T6.2
- **Files:** `abeomem/backup.py`, `abeomem/cli.py` (add `backup` command), `abeomem/server.py` (wire up startup check + asyncio task), `tests/unit/test_backup.py`
- **Goal:** Implements design.md §1.8.1 auto-backup **with fix #4 details**: asyncio task + `asyncio.to_thread` + dedicated connection + startup-check-is-guarantor semantics.
- **Impl notes:**
  - `run_backup(db_path, cfg)` is a blocking function opening a **fresh** connection, running `PRAGMA wal_checkpoint(TRUNCATE)` then `VACUUM INTO`, closing connection. The checkpoint is mandatory before VACUUM INTO.
  - Startup check: on `serve` startup, after migrations, check newest file in backup dir. If missing or older than `interval_days`, run backup synchronously before accepting tool calls.
  - Timer: `asyncio.create_task(backup_loop(...))` — `while True: await asyncio.sleep(interval); await asyncio.to_thread(run_backup, ...)`. Catch exceptions and log (don't crash server).
  - Rotation: after successful backup, list files by mtime; delete oldest until at `keep_count`.
  - `abeomem backup [--out PATH]` CLI runs `run_backup` synchronously. `--out` overrides target path.
- **Verify:**
  1. Fresh install: first `serve` start runs a backup; file exists.
  2. Second `serve` start within `interval_days` → no new backup.
  3. Rotation: set `keep_count=3`, run 5 backups → only 3 newest remain.
  4. Timer-triggered backup (use small interval for test) fires and runs on asyncio loop without blocking stdio.
  5. Backup running concurrently with `memory_save` → both succeed (dedicated connection).
- **Out of scope:** `backup --verify` (Stage 2).

### T6.5 — `abeomem init` + CLAUDE.md installer

- **Deps:** T6.3
- **Files:** `abeomem/claude_md.py`, `abeomem/cli.py` (add `init`), `tests/unit/test_init.py`
- **Goal:** Implements design.md §1.7. Idempotent install with markers; refuses in non-git dir without `--global`; shared-repo warning.
- **Impl notes:**
  - `abeomem init` (no flags): require git toplevel; refuse otherwise with the exact message from §1.7.
  - `abeomem init --global`: write to `~/.claude/CLAUDE.md`.
  - Marker pair `<!-- BEGIN abeomem -->` / `<!-- END abeomem -->`. Content between them is replaced; rest of file untouched.
  - If CLAUDE.md is git-tracked and has no markers: prompt `Continue? [y/N]`.
  - Also creates config + memos dir + DB on first run (calls migration runner).
- **Verify:**
  1. Fresh dir with git init → CLAUDE.md created with marker block; DB initialized; memos dir created.
  2. Re-run → CLAUDE.md unchanged (idempotent).
  3. Non-git dir without `--global` → refuses with message.
  4. Existing CLAUDE.md with arbitrary content → append block, preserve original content.
  5. Existing marker block → replace content between markers only.
- **Out of scope:** SessionStart hook install (never — §1.7).

---

## Phase 7 — Metrics

### T7.1 — `abeomem stats`

- **Deps:** T6.2
- **Files:** `abeomem/cli.py` (extend), `tests/unit/test_stats.py`
- **Goal:** Implements design.md §1.9 metrics table for fixed 30-day window. Handles zero-denominator and ANSI rendering rules.
- **Impl notes:**
  - 7 metrics from §1.9 table. Each is a SQL query against `memo_event` + `memo`.
  - **Filter `session_id NOT IN ('cli', 'watchdog')` for session-count metrics** (fix #2).
  - Zero-denominator → render as `—` (em dash).
  - Below-target rows → prefix `[ALERT]` and ANSI red when `sys.stdout.isatty()`; plain otherwise.
  - `--json` → JSONL with structured fields, no ANSI.
- **Verify:**
  1. Empty DB → all metrics render as `—`.
  2. `useful:retrieved` computed correctly over seeded events.
  3. Below-target `backups_last_30_days` → `[ALERT]` prefix in TTY mode.
  4. Piped output (`| cat`) → no ANSI codes.
  5. Session filter: 10 watchdog events + 2 MCP sessions → `retrievals_per_day` counts only MCP.
- **Out of scope:** time-windowed stats (Stage 2).

---

## Phase 8 — Acceptance suite

### T8.1–T8.6 — Acceptance criteria as automated tests

- **Deps:** all prior
- **Files:** `tests/acceptance/test_acceptance_1.py` through `_6.py`
- **Goal:** One test file per acceptance criterion from design.md §1 (acceptance). Each test must be runnable standalone and produce a clear pass/fail.

| # | Criterion | Test strategy |
|---|---|---|
| 1 | Two concurrent CC sessions in different repos save and search without contention errors | Spawn two subprocess `serve` instances against two separate repo scopes sharing one DB; fire 100 interleaved saves+searches via MCP; assert no errors and row counts add up. |
| 2 | DB consistent under crash; migrations all-or-nothing; backup+restore round-trips | SIGKILL mid-write → reopen, WAL recovery succeeds. Inject failing migration → `schema_version` unchanged. Backup, wipe DB, restore, assert identical rows. |
| 3 | Identical-content re-save in same scope returns `duplicate` | Save memo M, save M again (same scope) → status `duplicate`, same id, row count unchanged. |
| 4 | Concurrent `memory_save(supersedes=X)`: one wins, other gets `superseded_target` with winner's id | Already tested in T4.4 unit — lift to acceptance with two real MCP subprocesses. |
| 5 | External `.md` edit reflected in search within 2s, debounced to one update event | Write-storm 5 edits in 100ms; wait 2s; `memory_event` has exactly 1 update row for that memo with `session_id="watchdog"`; `memory_search` returns new content. |
| 6 | `abeomem stats` produces full metrics table on day 1 | Fresh install + 1 save + 1 search → stats shows all 7 rows; zero-denom rows render `—`. |

- **Verify:** `pytest tests/acceptance/ -v` — all six pass.
- **Out of scope:** performance benchmarks (Stage 4 gate).

---

## Parallelism summary (for dispatching multiple agents)

- **Phase 2 tasks (T2.1–T2.4)** are fully parallel after T0.1. Dispatch 4 agents.
- **Phase 4 tasks T4.4 and T4.7** can run in parallel after T4.3 (different files, no shared mutable state).
- **Phase 4 tasks T4.5 and T4.6** can run in parallel after T4.4 (different files; T4.5 extends save, T4.6 creates update).
- **Phase 5 tasks T5.1 and T5.2** are sequential (parse depends on export format agreement); T5.3 and T5.4 are sequential.
- **Phase 6 tasks T6.4 and T6.5** can run in parallel after T6.3.

Everywhere else, default to serial execution to keep dependency reasoning simple. Speculative parallelism isn't worth the merge pain.

---

## Task tracker

Mark progress inline as you go. Commit SHAs in parens after completion.

### Phase 0 — Scaffolding

- [ ] T0.1 — Project skeleton
- [ ] T0.2 — Lint + format baseline

### Phase 1 — Storage foundation

- [ ] T1.1 — Connection helper + bootstrap pragmas
- [ ] T1.2 — Migration runner
- [ ] T1.3 — Migration 001

### Phase 2 — Pure-logic utilities (parallel)

- [ ] T2.1 — `content_hash`
- [ ] T2.2 — `slugify`
- [ ] T2.3 — Scope resolution
- [ ] T2.4 — Topic normalization

### Phase 3 — Event plumbing

- [ ] T3.1 — `write_event` helper

### Phase 4 — MCP tools

- [ ] T4.1 — `memory_save` (minimal)
- [ ] T4.2 — `memory_get`
- [ ] T4.3 — `memory_search` (no boost)
- [ ] T4.4 — `memory_save` — supersede CAS
- [ ] T4.5 — `memory_save` — dedup
- [ ] T4.6 — `memory_update` (full, with fix #1 + #3)
- [ ] T4.7 — `memory_useful`
- [ ] T4.8 — `memory_search` — topic boost + `_hint`

### Phase 5 — Mirror + watchdog

- [ ] T5.1 — Markdown export
- [ ] T5.2 — Markdown parse
- [ ] T5.3 — Watchdog + debouncer (session_id="watchdog")
- [ ] T5.4 — Startup reconciliation

### Phase 6 — CLI + server bootstrap

- [ ] T6.1 — Config loader
- [ ] T6.2 — Server bootstrap + `serve`
- [ ] T6.3 — CLI commands (ls/show/edit/chain/archive/topics/sync/scope)
- [ ] T6.4 — Backup (asyncio, fix #4)
- [ ] T6.5 — `init` + CLAUDE.md installer

### Phase 7 — Metrics

- [ ] T7.1 — `abeomem stats`

### Phase 8 — Acceptance suite

- [ ] T8.1 — Two-session concurrency
- [ ] T8.2 — Crash/migration/backup
- [ ] T8.3 — Dedup round-trip
- [ ] T8.4 — Supersede race
- [ ] T8.5 — Watchdog 2s + debounce
- [ ] T8.6 — Stats day-1

---

## Completion

Plan is done when:

1. All tasks `[x]`.
2. `pytest tests/acceptance/` — all six pass.
3. `ruff check abeomem/ tests/` — zero findings.
4. `abeomem stats` on a real install shows all 7 rows rendered (even if mostly `—`).
5. A CC session with `claude mcp add abeomem -- uvx abeomem serve` can call `memory_save` and `memory_search` round-trip.

Ship Stage 1. Dogfood for 2 weeks. Revisit at the Stage 2 gate.
