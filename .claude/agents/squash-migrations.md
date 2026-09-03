---
name: squash-migrations
description: Squashes Django migrations to reduce fresh-database setup time, using scripts/squash_migrations.py (state-diff regeneration, not plain squashmigrations)
tools: Read, Edit, Bash, Glob
---

You are a specialized agent for squashing Django migrations in the Waldur project.
Use `scripts/squash_migrations.py`; read its module docstring first.

## Who runs a squash

Not only empty databases. Django uses a `replaces`-squash whenever **all or none** of
its replaced migrations are applied (`MigrationLoader.build_graph`). A deployment
upgrading from before the range applies the squash instead of the originals, and
`django_migrations` then records every original as applied. So a squash must do
everything the originals did to *data*, not just leave the same schema behind.
This was got wrong once (8.1.3-rc.7 shipped squashes without any `RunPython`; see
waldur-mastermind#347 and `marketplace/0281_rerun_data_migrations_skipped_by_squashes`),
and the test `SquashesKeepDataOperationsTest` now pins every `RunPython` / `RunSQL` of
the originals to its squash.

## What actually makes a fresh `migrate` fast

Fresh-database migration time is ~95% Python state rendering, ~2% Postgres. Every
*schema* operation (AddField, AlterField, CreateModel) re-renders a large part of the
FK-linked model graph, so cost ≈ number of operations × render time. **Fewer files
does not help; fewer schema operations does.** A kept `RunPython` costs one render at
most, and nothing on an empty database.

Plain `squashmigrations` concatenates operations and its optimizer cannot fold field
changes into `CreateModel` across FK-linked models, so a range squash keeps most of
its operations. The script therefore *regenerates* each squash: it walks the replaced
operations in plan order, keeps every `RunPython`, `RunSQL`, `SeparateDatabaseAndState`
and custom operation verbatim (their code is referenced from the original module via
the `_original()` helper it writes into the squash), and replaces each run of schema
operations in between with the autodetector diff of the states around it. Expect 3–5×
fewer schema operations, with the data operations exactly where they were.

Two things the segments need that a single migration does not give for free:

- `CreateModel` queues indexes and unique/FK constraints as *deferred* SQL that runs
  when the migration ends, so data code or a later segment touching that model would
  not see them. Every kept operation is preceded by
  `waldur_core.core.migration_operations.FlushDeferredSql`.
- The squash stays one transaction: a `RunPython` from an `atomic = False` original
  keeps working but loses its batch-by-batch resumability; DDL that cannot run in a
  transaction (`CONCURRENTLY`) makes the script refuse the range.
- A model deleted inside the range is deleted with `DeleteModel` alone (the originals
  did the same); the per-field teardown the autodetector emits before it introspects
  objects the history may never have created.
- A segment whose diff would change a column's type (a rename + add + copy + remove
  around a data step collapses into one `AlterField` whose cast Postgres may refuse)
  is emitted as the originals' own schema operations instead of the diff.

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

# regenerate an existing replaces-squash in place (same range)
uv run python scripts/squash_migrations.py regen marketplace 0132_squashed_0132_0179
```

The script hides superseded `replaces`-squashes inside the range, repoints any
dependency on their names to the replaced set's leaf, runs `squashmigrations` for the
dependencies and `replaces` list, regenerates the operations from the originals, adds
a dependency on every app a kept `RunPython` reads (`get_model()` calls; the originals
often relied on plan order for that, which a squash changes), lints, and validates the
graph plus `makemigrations --check`. On failure it reverts everything it touched.

### 3. DDL that an original issued from Python

`RunSQL` of the originals is kept automatically. What the regeneration cannot see is
DDL executed through `cursor.execute` inside a `RunPython`: the squash still runs
that code on `migrate`, but `migrate_fresh` skips `RunPython` and replays only DDL
`RunSQL`, so such DDL must also exist as a `RunSQL` in the squash. Hand-added
`RunSQL` survives regeneration (statements already carried by an original are
dropped, so nothing runs twice). Known case: the `logging` expression indexes.
Grep the replaced range for `cursor.execute` and check each one.

### 4. Verify

```bash
# fresh replay + timing (auto-starts a temporary Postgres container)
uv run python scripts/test_fresh_migrations.py

# the data-operation and DDL pins
DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local uv run pytest src/waldur_core/core/tests/test_migrate_fresh.py

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

- The originals a squash replaces must stay in place: Django needs them for
  partially-applied databases, and the squash imports its data code from them.
- If an app has no originals for a squash (they were deleted), the file is a plain
  migration: drop its `replaces` and repoint dependencies to the squash name —
  otherwise `sqlmigrate` breaks for the whole project.
- Dependencies on a squash *name* (rather than an original) break re-squashing;
  point them at originals.
- Don't squash `auth`, `contenttypes`, `axes`, `reversion` or other third-party apps.
- `elidable=True` is ignored by the regeneration and means nothing here: a data
  migration is needed by every database that upgrades across it. Don't mark them.
- A `RunPython` that reads another app's models should declare a dependency on that
  app in the original migration; the regeneration infers one, but explicit is better.
- Squashes span to each app's leaf, so a local DB that is a few migrations behind
  makes `makemigrations` raise `InconsistentMigrationHistory` (Django resolves the
  graph without DB knowledge). Run `waldur migrate` first; that is expected.
