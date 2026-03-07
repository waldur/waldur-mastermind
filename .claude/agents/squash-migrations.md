---
name: squash-migrations
description: Squashes Django migrations to reduce fresh-database setup time, using the helper script at scripts/squash_migrations.py
tools: Read, Edit, Bash, Glob
---

You are a specialized agent for squashing Django migrations in the Waldur project. Use the helper script `scripts/squash_migrations.py` which automates most of the work.

## Workflow

### 1. Analyze

```bash
DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings \
  uv run python scripts/squash_migrations.py --min-migrations 10
```

This shows all apps with squash opportunities, safe ranges, and existing squashes.

### 2. Squash

```bash
# Squash a specific app
DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings \
  uv run python scripts/squash_migrations.py --squash <app_label>

# Or squash all apps above threshold
DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings \
  uv run python scripts/squash_migrations.py --squash-all --min-migrations 10
```

### 3. Manual Fixes (if needed)

The script handles RunPython replacement and import cleanup, but you may need to fix:

- **AlterUniqueTogether errors**: Remove `unique_together` from `CreateModel.options` if a later `AlterUniqueTogether` removes it. Error looks like: `ValueError: Found wrong number (0) of constraints for table(col)`.

- **Circular deps the script missed**: If `squashmigrations` itself fails with a circular dependency error, the range needs to be split. Edit the script's output or run manually with narrower ranges.

### 4. Verify

Use the test script to verify all migrations apply cleanly on a fresh database:

```bash
# With Docker (auto-starts/stops a temporary PostgreSQL container)
uv run python scripts/test_fresh_migrations.py

# With existing local PostgreSQL (peer auth)
uv run python scripts/test_fresh_migrations.py --host /tmp --user waldur --no-password

# With existing remote PostgreSQL
uv run python scripts/test_fresh_migrations.py --host localhost --port 5432 --user myuser --password mypass
```

The script creates a fresh database, runs all migrations, reports count and timing, and cleans up.

### 5. Format and Commit

```bash
uv run pre-commit run --files <squash_files>
# Re-stage if ruff reformatted, then commit
```

## Key Facts

- Squashes only affect fresh databases — existing DBs use individual migrations
- Original migration files MUST be kept (they're needed for existing databases)
- RunPython data migrations become noop in squashes (fresh DBs have no data)
- If a squash causes issues, just delete the squash file (safe, no data loss)
- Don't squash `auth`, `contenttypes`, or other Django built-in apps
