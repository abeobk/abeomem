# abeomem

MCP server that stores and retrieves tips, tricks, and bug fixes Claude Code already paid for once, so it doesn't pay again.

Stage 1 is shipped: five tools, four kinds of memo, SQLite+FTS5 storage with a markdown mirror, scope-aware retrieval, editor-friendly workflow. See [design.md](design.md) for the full spec (v2.4.2) and [plan.md](plan.md) for the build plan.

---

## The one-sentence target

**When CC is about to repeat a mistake, the relevant memo surfaces before the mistake happens.**

Everything else follows from that.

---

## Install

```bash
# From this repo
pip install dist/abeomem-0.1.0-py3-none-any.whl

# Per-project install (run inside a git repo)
abeomem init

# Or global install
abeomem init --global

# Register with Claude Code
claude mcp add abeomem -- abeomem serve
```

`abeomem init` creates the DB at `~/.abeomem/kb.db`, the mirror at `~/.abeomem/memos/`, the backup dir at `~/.abeomem/backups/`, and writes the memory instructions block into `CLAUDE.md` (repo-level or `~/.claude/CLAUDE.md` with `--global`).

After this, CC will automatically search and save during your sessions. You'll touch abeomem CLI only for curation.

---

## How CC uses it (automatic, via CLAUDE.md trigger)

Five MCP tools, prefixed `memory_`:

| Tool | When CC fires it |
|---|---|
| `memory_search` | Before debugging an error, before making a non-obvious choice, when seeing a new error this session |
| `memory_save` | Bug took >5 min to diagnose; discovered a non-obvious convention; made a decision worth revisiting |
| `memory_update` | Refine an existing memo — typo, clarification, extra notes, added tag |
| `memory_get` | Fetch full memo by id (after a search result) |
| `memory_useful` | **Only** after the user confirms a memo helped (CC must ask first) |

The CLAUDE.md block installed by `abeomem init` spells all this out for CC.

---

## Using abeomem efficiently

### Four kinds — pick the right one

| Kind | Shape | Example |
|---|---|---|
| `fix` | symptom + cause + solution | "TS build fails after pnpm update / `.pnpm` corrupted / `rm -rf node_modules && pnpm i`" |
| `gotcha` | symptom (cause/solution optional) | "`JSON.stringify` drops undefined values" |
| `convention` | rule (+ rationale) | "this repo uses pnpm workspaces — never npm" |
| `decision` | rule + rationale | "chose CockroachDB over Postgres — compliance requires multi-region" |

Different kinds get different BM25 ranking weights. Wrong kind → weaker retrieval.

### Topic hygiene (biggest retrieval win)

Topics affect ranking; tags don't.

- **Always pass topics on save**: `topics: ["python", "asyncio"]`, `topics: ["nginx", "ssl"]`.
- **Reuse before inventing.** Check `abeomem topics` first. A vocabulary of 20 reused topics beats 200 near-synonyms.
- **Normalization is automatic**: `"Python"` → `"python"`, `"memory leak"` → `"memory-leak"`. But consistent naming from the source reduces typos.
- **Cross-cutting concerns**: pass the protocol without the language. A memo with `[nginx, ssl, http2]` still matches a query with `[ssl]` (asymmetric boost — extra memo topics don't penalize).

### When to save (vs. skip)

Save | Skip
---|---
Bug took >5 min to diagnose | Bug solved by re-reading the error
A project rule not written in docs | Rule already in README.md or an ADR
An architectural choice you'd forget in a month | Today's scratchpad
An API quirk that bit you | Standard usage that's in the library docs

If CC over-saves, `save_dedup_rate` in `abeomem stats` climbs — the fuzzy dedup (RapidFuzz ≥85) catches redundant saves automatically. High dedup rate after warmup means CC is finding old memos on search, which is the goal.

### Update vs. supersede — the decision

| Change | Use | Why |
|---|---|---|
| Typo, clarification, added tag, extra notes | `memory_update` | Same memo, refined in place |
| Missed a cause or edge case | `memory_update` | Additive refinement |
| Project migrated npm → pnpm; old memo now wrong | `memory_save(supersedes=<id>)` | Preserves audit trail — old memo stays for history; search only sees the tip |
| Decision reversed | `memory_save(supersedes=<id>)` | Same reason |

Supersede chains are linear and walkable (`abeomem chain <id>`). CAS prevents races — if two CC sessions try to supersede the same memo, exactly one wins.

### Useful feedback — user rates, CC messengers

`memory_useful` is **load-bearing**. It feeds ranking (memos with higher `useful_count` surface more often) and the `useful:retrieved` metric is the only one worth optimizing.

Rule: CC must **ask the user** — "Did memo #47 help?" — before calling. Self-rating contaminates the signal because CC is optimistic about its own reasoning. Lower volume of honest ratings beats high volume of noise.

If your `useful:retrieved` is below 0.1 sustained, CC isn't asking. Fix the trigger (check CLAUDE.md has the abeomem block), don't fiddle with ranking.

### Scope — what gets saved where

Three scopes, auto-resolved from cwd:

- **`repo:<hash>`** — git repo with remote. Same remote = same scope across clones.
- **`repo:path:<hash>`** — git repo without remote (local-only).
- **`global`** — not in a git repo. Anchor is CWD.

`memory_save` always writes to the server's current scope (no scope parameter — deliberately). `memory_search` defaults to `scope="both"` which searches repo + global.

To get a separate scope for a non-git project: `git init` inside it. To see the current scope: `abeomem scope`.

---

## CLI cheat sheet

```bash
abeomem init [--global]         # setup; writes CLAUDE.md block
abeomem serve                   # MCP server (stdio) — invoked by claude mcp
abeomem scope [--show-remote]   # print current-directory scope + remote

# Inspecting
abeomem ls [--kind fix] [--topic python] [--scope all]
abeomem show <id>               # raw markdown to stdout
abeomem topics --min-count 2    # used topics, sorted by frequency
abeomem chain <id>              # supersede history (root → tip)

# Curating (CC never does these — human decision)
abeomem edit <id>               # open .md in $EDITOR; watchdog reimports on save
abeomem archive <id> --reason "dup of 89"
abeomem sync                    # rescan mirror → DB
abeomem sync --import-new       # ingest hand-written .md files

# Ops
abeomem backup                  # checkpoint WAL + VACUUM INTO dated file
abeomem stats                   # 30-day metrics
abeomem stats --json            # same, JSONL for scripting
```

All CLI write commands use `session_id="cli"`. Metrics filter these out when counting MCP user sessions.

---

## The markdown mirror — edit memos in your editor

Every memo lives in both SQLite (source of truth) and a `.md` file under `~/.abeomem/memos/<scope>/<kind>/<id>-<slug>.md`. Edit the file in Obsidian, VS Code, vim — the watchdog picks up saves within 500ms, debounced, and routes them through `memory_update` with `session_id="watchdog"`.

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

Filename slugs never change across title edits — git history on your memos dir stays stable.

Archived memos keep their file but gain an `archived_at:` frontmatter field and a banner in the body. Search never returns them; `abeomem ls --include-archived` does.

---

## Metrics (what to watch)

`abeomem stats` — fixed 30-day window. Seven metrics, only three matter for tuning:

- **`useful:retrieved`** — target >0.3 under honest user-confirmed policy; 0.15–0.30 is realistic; below 0.1 means CC isn't asking (trigger broken).
- **`save_dedup_rate`** — high after warmup is **good**: it means CC finds old memos on search.
- **`backups_last_30_days`** — should be ≥4 with default weekly backups. Below 4 prints `[ALERT]`.

Zero-denominator metrics render as `—`, never `0.0`. CLI output is ANSI-colored on TTY, plain when piped.

---

## Files and ownership

```
~/.abeomem/
├── kb.db                    # SQLite WAL — source of truth, never delete
├── kb.db-wal                # WAL — part of the DB, never delete
├── kb.db-shm                # shared memory — part of the DB, never delete
├── memos/                   # mirror, regenerable from DB
│   ├── global/
│   │   ├── fix/<id>-<slug>.md
│   │   ├── gotcha/...
│   │   └── ...
│   └── repo-<hash>/
│       └── ...
└── backups/
    └── kb-YYYYMMDD-HHMMSS-<μs>.db
```

Loss of `memos/` is annoying (re-exported on next server startup). Loss of `kb.db` + all backups is fatal. Put `~/.abeomem/` in Syncthing, git, or your cloud backup of choice.

---

## Backup & migration

`abeomem backup` is the only correct way to snapshot a live DB. It runs `PRAGMA wal_checkpoint(TRUNCATE)` then `VACUUM INTO` on a dedicated connection — the result is a single self-contained, defragmented `.db` file with every WAL page merged in. A plain `cp kb.db` while the server is running can miss uncheckpointed pages and give you a corrupt snapshot.

Backups also run automatically: on `serve` startup if the newest backup is older than `interval_days` (default 7), and on a background timer at the same interval. Last 8 backups are kept (`keep_count`).

**Don't zip the whole `~/.abeomem/` dir.** `memos/` is regeneratable from the DB. `backups/` is redundant. Raw `kb.db` + `-wal` + `-shm` copied live can be inconsistent. Just back up the one VACUUMed `.db`.

**Migrate to another machine:**

```bash
# On source
abeomem backup --out /tmp/kb-migrate.db
scp /tmp/kb-migrate.db new-host:~/

# On target (after `pip install` + `abeomem init --global`)
mv ~/kb-migrate.db ~/.abeomem/kb.db   # replaces the fresh empty DB
abeomem sync                          # re-exports memos/ from the DB
```

If you customized `~/.config/abeomem/config.toml`, copy that too. Keep both machines on the same abeomem version so migrations don't diverge — if they do, the newer version will auto-upgrade the DB on first run; downgrade isn't supported.

---

## Design invariants (don't violate)

- The user is the rater for `memory_useful`. CC asks; user answers; CC relays.
- Archive is reversible; delete isn't. No hard delete.
- No SessionStart injection. Recall is pull, not push.
- `abeomem serve` never writes to stdout — stdio owns it for JSON-RPC.
- Markdown is the mirror; the DB is the source of truth.

---

## Development

```bash
pip install -e '.[dev]'
pytest                          # 200 tests
ruff check abeomem/ tests/      # lint
python -m build --wheel         # build wheel
```

Layout: [abeomem/](abeomem/) package, [tests/unit/](tests/unit/) per-module tests, [tests/acceptance/](tests/acceptance/) end-to-end tests mapping to `design.md` §1 acceptance criteria.

---

## Not shipping in Stage 1

By design, none of these exist yet. They're gated on real dogfood signal — see the staging table in `design.md`:

- Structured `refs` between memos
- `abeomem merge`, `abeomem prune`, `backup --verify`
- Consolidation, review sessions, term reinforcement
- PreToolUse hook
- Embeddings, hybrid retrieval
- Multi-machine sync, team sharing, web UI

The goal is to use Stage 1 for two weeks, then decide what actually earns the next feature based on what the metrics show — not what sounds good.
