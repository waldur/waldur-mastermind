---
name: squash-migrations
description: Squashes Django migrations to reduce fresh-database setup time, using scripts/squash_migrations.py (state-diff regeneration, not plain squashmigrations)
tools: Read, Edit, Bash, Glob
---

You are a specialized agent for squashing Django migrations in the Waldur project.
Use `scripts/squash_migrations.py`; read its module docstring first.

## What actually makes a fresh `migrate` fast

Fresh-database migration time is ~95% Python state rendering, ~2% Postgres. Every
*operation* (AddField, AlterField, CreateModel, even a no-op RunPython) re-renders a
large part of the FK-linked model graph, so cost ≈ number of operations × render time.
**Fewer files does not help; fewer operations does.**

Plain `squashmigrations` concatenates operations and its optimizer cannot fold field
changes into `CreateModel` across FK-linked models, so a range squash keeps most of
its operations. The script therefore *regenerates* each squash as an autodetector
state diff (before the range → after the range, or → the real models when the range
ends at the app's leaf). Expect 3–5× fewer operations.

## Workflow

### 1. Analyze

```bash
uv run python scripts/squash_migrations.py analyze            # all apps
uv run python scripts/squash_migrations.py analyze marketplace
```

Shows, per app, the longest cycle-free ranges and which external migrations block
extending them. Apps that interleave with another app (openstack ↔ openstack_tenant,
marketplace ↔ structure/invoices/policy/support) cannot be squashed whole.

### 2. Squash

```bash
# whole app -> one migration (fails and reverts cleanly on a cross-app cycle)
uv run python scripts/squash_migrations.py app waldur_rancher

# a cycle-free range from `analyze`
uv run python scripts/squash_migrations.py range marketplace 0226_alter_offeringcomponent_limit_period 0263_remove_category_default_tenant_category

# shrink an existing replaces-squash in place (same range, fewer operations)
uv run python scripts/squash_migrations.py regen marketplace 0132_squashed_0132_0179
```

The script hides superseded `replaces`-squashes inside the range, repoints any
dependency on their names to the replaced set's leaf, runs `squashmigrations`,
drops RunPython/RunSQL, regenerates the operations, lints, and validates the graph
plus `makemigrations --check`. On failure it reverts everything it touched.

### 3. Re-add DDL that lives only in RunSQL

Data migrations are correctly dropped (a fresh DB has no data), but DDL-only RunSQL
is not on the model and must be copied into the squash by hand. Known cases:
`logging` expression indexes (`log_event_org/proj/user`), the marketplace SLURM
partition trigger (`0254`). Grep the replaced range for `RunSQL` and
`cursor.execute` and check each one.

### 4. Verify

```bash
# fresh replay + timing (auto-starts a temporary Postgres container)
uv run python scripts/test_fresh_migrations.py

# schema equivalence: dump a fresh DB before and after, then
uv run python scripts/squash_migrations.py compare-schema before.sql after.sql
```

Only Django-generated constraint/index *names* may differ (tables renamed in
history get fresh hashes). Any missing index, column, trigger or default is a bug.

### 5. Format and commit

```bash
uvx prek run --files <touched files>
```

## Key facts

- Squashes only affect fresh databases; existing DBs keep using the original
  migrations, which must stay in place. Deleting a superseded squash file is safe.
- If an app has no originals for a squash (they were deleted), the file is a plain
  migration: drop its `replaces` and repoint dependencies to the squash name —
  otherwise `sqlmigrate` breaks for the whole project.
- Dependencies on a squash *name* (rather than an original) break re-squashing;
  point them at originals.
- Don't squash `auth`, `contenttypes`, `axes`, `reversion` or other third-party apps.
- Mark new data migrations `RunPython(..., elidable=True)`.
- Squashes span to each app's leaf, so a local DB that is a few migrations behind
  makes `makemigrations` raise `InconsistentMigrationHistory` (Django resolves the
  graph without DB knowledge). Run `waldur migrate` first; that is expected.
- `regen`/`range` keep any `RunSQL` already in the squash; a test asserts every
  DDL `RunSQL` of the replaced originals is present in its squash.
