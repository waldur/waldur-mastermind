#!/usr/bin/env python3
"""
Squash Django migrations so that a fresh database migrates faster.

Why this script exists (measured 2026-08): a fresh `waldur migrate` spends ~95% of
its time in Python (``ProjectState.render_multiple`` / ``reload_model``), not in
Postgres. Every operation - AddField, AlterField, CreateModel, even a no-op
RunPython - re-renders a large part of the ~400 FK-linked models, so the cost of the
fresh chain is roughly ``number_of_operations x render_time``. Plain
``squashmigrations`` barely helps: its optimizer refuses to fold Add/AlterField into
CreateModel across operations that reference the model, so a 48-migration range
still comes out as ~200 operations.

The approach here is to *regenerate* a squash's operations as the autodetector diff
between the project state just before the range and the state just after it (or the
real models, when the range ends at the app's last migration). That folds every
field change into CreateModel and typically cuts operations 3-5x.

Commands:

    # Where can an app be squashed without a cross-app cycle?
    uv run python scripts/squash_migrations.py analyze [app ...]

    # Squash an app's entire history into one migration (fails cleanly on a cycle)
    uv run python scripts/squash_migrations.py app <app_label>

    # Squash a range; regenerates in place if a squash with that exact range exists
    uv run python scripts/squash_migrations.py range <app_label> <start> <end>

    # Schema equivalence: snapshot two databases (names of auto-generated constraints
    # are ignored) and diff them - used by CI to prove `migrate_fresh` == `migrate`
    uv run python scripts/squash_migrations.py snapshot-db > migrated.txt
    uv run python scripts/squash_migrations.py compare-schema migrated.txt fresh.txt

Validate afterwards with ``scripts/test_fresh_migrations.py`` (fresh replay + timing)
and ``waldur makemigrations --check`` (the commands above already run the latter).

Things the regeneration cannot know, and which you must re-add by hand:

- DDL-only ``RunSQL`` (expression indexes, triggers, functions). Data-only
  RunSQL/RunPython are correctly dropped: a fresh database has no data.
- Anything else that is not expressible on the model.

Existing databases are unaffected: they keep applying the original migrations, which
stay in place. Only ``replaces``-squashes are hidden/replaced by this script.

Environment: DJANGO_SETTINGS_MODULE (defaults to waldur_core.server.test_settings).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "waldur_core.server.test_settings")

import django  # noqa: E402

django.setup()

from django.apps import apps as global_apps  # noqa: E402
from django.db.migrations import (
    Migration,  # noqa: E402
    RunSQL,  # noqa: E402
)
from django.db.migrations.autodetector import MigrationAutodetector  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402
from django.db.migrations.operations import RenameModel  # noqa: E402
from django.db.migrations.questioner import MigrationQuestioner  # noqa: E402
from django.db.migrations.state import ProjectState  # noqa: E402
from django.db.migrations.writer import MigrationWriter  # noqa: E402

OP_RE = re.compile(r"^        migrations\.[A-Z]", re.M)
REPLACES_RE = re.compile(r"^    replaces\s*=\s*\[(.*?)\]", re.S | re.M)
NAME_RE = re.compile(r'"(\d{4,}[a-z0-9_]*)"')
HIDE_DIR = Path(".squash-hidden")


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(
            " ".join(cmd[-3:]) + "\n" + (result.stderr or result.stdout)[-1500:]
        )
    return result


# --------------------------------------------------------------------------- graph


def originals_graph():
    """Graph of ORIGINAL migrations only (squash nodes removed, their children remapped)."""
    loader = MigrationLoader(None, replace_migrations=False)
    graph = loader.graph
    for key, migration in loader.replacements.items():
        if key in graph.nodes:
            graph.remove_replacement_node(key, migration.replaces)
    return loader, graph


def app_nodes(graph, app: str) -> list[tuple[str, str]]:
    return sorted(key for key in graph.nodes if key[0] == app)


def cut_points(app: str) -> list[tuple[str, str, int, list[str]]]:
    """Greedy: the longest prefix squashable without a cross-app cycle, then repeat."""
    _, graph = originals_graph()
    nodes = app_nodes(graph, app)
    ancestors = {n: set(graph.forwards_plan(n)) - {n} for n in graph.nodes}

    def blockers(segment):
        segment = set(segment)
        needed = set().union(*(ancestors[n] for n in segment)) - segment
        return sorted(f"{x[0]}.{x[1]}" for x in needed if ancestors[x] & segment)[:3]

    ranges, i = [], 0
    while i < len(nodes):
        best = i
        for k in range(len(nodes) - 1, i - 1, -1):
            if not blockers(nodes[i : k + 1]):
                best = k
                break
        blocked_by = blockers(nodes[i : best + 2]) if best + 1 < len(nodes) else []
        ranges.append((nodes[i][1], nodes[best][1], best - i + 1, blocked_by))
        i = best + 1
    return ranges


# ---------------------------------------------------------------------- regenerate


class _KeepRenames(MigrationQuestioner):
    """Answer 'yes' to rename questions so renames stay RenameModel/RenameField."""

    def ask_rename(self, *args):
        return True

    def ask_rename_model(self, *args):
        return True


def _normalize(state: ProjectState) -> None:
    # Historical migrations may declare unique_together as list-of-lists.
    for model_state in state.models.values():
        for key in ("unique_together", "index_together"):
            if model_state.options.get(key):
                model_state.options[key] = {tuple(x) for x in model_state.options[key]}


def regenerate(
    app: str, squash_name: str, start: str, end: str, to_models: bool
) -> int:
    """Rewrite ``squash_name``'s operations as the state diff over [start..end].

    States are built from original migrations:
      from = all nodes - range - descendants(range)
      to   = all nodes - descendants(range)   (or the real models when ``to_models``)
    Dependencies and ``replaces`` of the existing squash file are kept.
    """
    existing = MigrationLoader(None).get_migration(app, squash_name)
    loader, graph = originals_graph()
    nodes = app_nodes(graph, app)
    first = 0 if start == "none" else nodes.index((app, start))
    rng = set(nodes[first : nodes.index((app, end)) + 1])
    descendants = set().union(*(set(graph.backwards_plan(n)) for n in rng)) - rng
    everything = set(graph.nodes)

    def state(keys):
        if not keys:
            return ProjectState()
        return graph.make_state(
            nodes=sorted(keys), at_end=True, real_apps=loader.unmigrated_apps
        )

    from_state = state(everything - rng - descendants)
    to_state = (
        ProjectState.from_apps(global_apps)
        if to_models
        else state(everything - descendants)
    )
    _normalize(from_state)
    _normalize(to_state)

    # A RenameModel also rewrites FKs in *other* apps' state; a Delete+Create diff
    # cannot reproduce that, so replay the range's renames first and diff from there.
    renames = [
        op
        for key in graph.forwards_plan(max(rng))
        if key in rng
        for op in graph.nodes[key].operations
        if isinstance(op, RenameModel)
    ]
    renames = [r for r in renames if (app, r.old_name_lower) in from_state.models]
    for rename in renames:
        rename.state_forwards(app, from_state)

    changes = MigrationAutodetector(
        from_state, to_state, questioner=_KeepRenames()
    )._detect_changes()
    ops = renames + [op for m in changes.get(app, []) for op in m.operations]
    # DDL-only RunSQL (expression indexes, triggers) is hand-added to squashes and
    # must survive regeneration; the autodetector knows nothing about it.
    ops += [op for op in existing.operations if isinstance(op, RunSQL)]
    new = type(
        "Migration",
        (Migration,),
        {
            "operations": ops,
            "dependencies": existing.dependencies,
            "replaces": existing.replaces,
            "initial": existing.initial,
        },
    )(squash_name, app)
    Path(MigrationWriter(existing).path).write_text(MigrationWriter(new).as_string())
    return len(ops)


def strip_run_ops(path: Path) -> int:
    """Remove RunPython/RunSQL operations from a freshly generated squash.

    Done on the AST so string literals containing parentheses cannot confuse it.
    Formatting/comments are lost, which is fine: regenerate() rewrites the file.
    """
    import ast

    tree = ast.parse(path.read_text())
    removed = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "operations" for t in node.targets
            )
            and isinstance(node.value, ast.List)
        ):
            keep = []
            for elt in node.value.elts:
                func = getattr(elt, "func", None)
                if isinstance(func, ast.Attribute) and func.attr in (
                    "RunPython",
                    "RunSQL",
                ):
                    removed += 1
                else:
                    keep.append(elt)
            node.value.elts = keep
    path.write_text(ast.unparse(tree) + "\n")
    return removed


# ------------------------------------------------------------------ squash driver


def _num(path: Path) -> int:
    return int(re.match(r"(\d+)", path.stem).group(1))


def _replaces_of(path: Path) -> list[str]:
    match = REPLACES_RE.search(path.read_text())
    return NAME_RE.findall(match.group(1)) if match else []


def _leaf(graph, app: str, names: list[str]) -> str:
    """The replaced migration nothing else in the replaced set depends on."""
    parents = {
        n: {p.key for p in graph.node_map[(app, n)].parents}
        for n in names
        if (app, n) in graph.node_map
    }
    leaves = [n for n in names if not any((app, n) in ps for ps in parents.values())]
    if len(leaves) != 1:
        raise RuntimeError(f"cannot determine leaf of replaced set {names}: {leaves}")
    return leaves[0]


def _validate() -> None:
    run(
        sys.executable,
        "-c",
        "import django; django.setup()\n"
        "from django.db.migrations.loader import MigrationLoader\n"
        "g = MigrationLoader(None).graph; g.validate_consistency(); g.ensure_not_cyclic()",
    )
    result = run(
        "uv", "run", "waldur", "makemigrations", "--check", "--dry-run", check=False
    )
    if "No changes detected" not in result.stdout + result.stderr:
        raise RuntimeError(
            "migration state drifted from models:\n" + result.stdout[-800:]
        )


def squash_range(app: str, start: str | None, end: str | None) -> str:
    """Squash originals [start..end] (defaults: whole app) into one regenerated migration."""
    mdir = Path(global_apps.get_app_config(app).path) / "migrations"
    files = sorted(mdir.glob("[0-9]*.py"), key=lambda p: (_num(p), p.stem))
    squashes = {p.stem: _replaces_of(p) for p in files if _replaces_of(p)}
    originals = [p.stem for p in files if p.stem not in squashes]
    start, end = start or originals[0], end or originals[-1]
    for name in (start, end):
        if name not in originals:
            raise SystemExit(
                f"{name} is not an original migration of {app} (squash names are not accepted)"
            )
    first, last = originals.index(start), originals.index(end)
    is_leaf = last == len(originals) - 1
    _, ograph = originals_graph()

    hidden: list[str] = []
    edited: dict[Path, str] = {}
    created: Path | None = None
    backup: str | None = None
    target: Path | None = None

    def revert():
        for path, text in edited.items():
            path.write_text(text)
        for name in hidden:
            shutil.move(HIDE_DIR / app / f"{name}.py", mdir / f"{name}.py")
        if created and created.exists():
            created.unlink()
        if backup is not None and target is not None:
            target.write_text(backup)

    try:
        in_place = next(
            (s for s, r in squashes.items() if r[0] == start and r[-1] == end), None
        )
        if in_place:
            name = in_place
        else:
            covered = set(originals[first : last + 1])
            for name_, replaced in squashes.items():
                if not set(replaced) & covered:
                    continue
                if not set(replaced) <= covered:
                    raise RuntimeError(f"existing squash {name_} straddles the range")
                leaf = _leaf(ograph, app, replaced)
                pattern = re.compile(r'\(\s*"%s",\s*"%s",?\s*\)' % (app, name_))
                for path in Path("src").rglob("migrations/*.py"):
                    text = path.read_text()
                    new_text = pattern.sub(f'("{app}", "{leaf}")', text)
                    if new_text != text:
                        edited[path] = text
                        path.write_text(new_text)
                (HIDE_DIR / app).mkdir(parents=True, exist_ok=True)
                shutil.move(mdir / f"{name_}.py", HIDE_DIR / app / f"{name_}.py")
                hidden.append(name_)
            before = set(mdir.glob("*.py"))
            run(
                "uv", "run", "waldur", "squashmigrations", app, start, end,
                "--noinput", "--squashed-name", f"squashed_{end[:4]}",
            )  # fmt: skip
            made = set(mdir.glob("*.py")) - before
            if len(made) != 1:
                raise RuntimeError(
                    f"squashmigrations did not create exactly one file: {made}"
                )
            created = made.pop()
            name = created.stem
            strip_run_ops(created)
        target = mdir / f"{name}.py"
        backup = target.read_text() if in_place else None
        ops = regenerate(app, name, start, end, to_models=is_leaf)
        run("uvx", "ruff", "check", "--fix", "-q", str(target), check=False)
        run("uvx", "ruff", "format", "-q", str(target))
        _validate()
    except Exception:
        revert()
        raise
    for name_ in hidden:
        (HIDE_DIR / app / f"{name_}.py").unlink()
    return (
        f"{app} {start}..{end} -> {name} ({ops} ops from {last - first + 1} migrations"
        f"{', in place' if in_place else ''}; removed old squashes {hidden or 'none'})"
    )


# ------------------------------------------------------------------ schema compare


def snapshot_db(alias: str = "default") -> list[str]:
    """Normalized description of the connected database's schema, one line per fact.

    Auto-generated constraint/index names are dropped (they depend on migration
    history), everything structural is kept: columns, types, nullability, defaults,
    index definitions, constraint definitions, triggers, functions, and the row count
    of every table (rows created by post_migrate receivers must match too).
    """
    from django.db import connections

    lines = []
    with connections[alias].cursor() as cursor:
        cursor.execute(
            """SELECT table_name, column_name, data_type, character_maximum_length,
                      numeric_precision, numeric_scale, is_nullable, column_default
               FROM information_schema.columns WHERE table_schema = 'public'"""
        )
        for (
            table,
            column,
            dtype,
            length,
            prec,
            scale,
            nullable,
            default,
        ) in cursor.fetchall():
            if table == "django_migrations":
                continue
            default = re.sub(
                r"nextval\('[^']+'::regclass\)", "nextval(<seq>)", default or ""
            )
            lines.append(
                f"column {table}.{column} {dtype} len={length} prec={prec},{scale} null={nullable} default={default}"
            )
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        for (table,) in cursor.fetchall():
            # constance rows appear lazily on any config read, not through migrations
            if table in ("django_migrations", "constance_constance"):
                continue
            cursor.execute(f'SELECT count(*) FROM "{table}"')
            lines.append(f"rows {table} {cursor.fetchone()[0]}")
        cursor.execute(
            "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'public'"
        )
        for table, indexdef in cursor.fetchall():
            if table == "django_migrations":
                continue
            indexdef = re.sub(
                r"^CREATE (UNIQUE )?INDEX \S+ ON ", r"CREATE \1INDEX ON ", indexdef
            )
            lines.append(f"index {indexdef}")
        cursor.execute(
            """SELECT c.conrelid::regclass, c.contype, pg_get_constraintdef(c.oid)
               FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
               WHERE n.nspname = 'public'"""
        )
        for table, contype, definition in cursor.fetchall():
            if str(table) == "django_migrations":
                continue
            lines.append(f"constraint {table} {contype} {definition}")
        cursor.execute(
            """SELECT tgrelid::regclass, tgname, pg_get_triggerdef(oid)
               FROM pg_trigger WHERE NOT tgisinternal"""
        )
        for table, name, definition in cursor.fetchall():
            lines.append(f"trigger {table} {name} {definition}")
        cursor.execute(
            """SELECT p.proname, pg_get_function_identity_arguments(p.oid)
               FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"""
        )
        for name, args in cursor.fetchall():
            lines.append(f"function {name}({args})")
    return sorted(lines)


def compare_schema(before: Path, after: Path) -> int:
    a, b = set(before.read_text().splitlines()), set(after.read_text().splitlines())
    for line in sorted(a - b):
        print("- " + line)
    for line in sorted(b - a):
        print("+ " + line)
    print(f"{len(a - b)} only in {before.name}, {len(b - a)} only in {after.name}")
    return 1 if a != b else 0


# ------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("analyze", help="show cycle-free squash ranges per app")
    p.add_argument(
        "apps", nargs="*", help="app labels (default: every app with >= 5 migrations)"
    )
    p = sub.add_parser("app", help="squash an app's whole history into one migration")
    p.add_argument("app")
    p = sub.add_parser("range", help="squash a range of original migrations")
    p.add_argument("app")
    p.add_argument("start")
    p.add_argument("end")
    p = sub.add_parser(
        "regen", help="regenerate an existing squash's operations in place"
    )
    p.add_argument("app")
    p.add_argument("squash_name")
    p = sub.add_parser(
        "snapshot-db",
        help="print a normalized schema snapshot of the configured database",
    )
    p.add_argument("--database", default="default")
    p = sub.add_parser("compare-schema", help="diff two snapshot-db outputs")
    p.add_argument("before", type=Path)
    p.add_argument("after", type=Path)
    args = parser.parse_args()

    if args.command == "analyze":
        graph = MigrationLoader(None).graph
        labels = args.apps or sorted(
            {
                k[0]
                for k in graph.nodes
                if len(app_nodes(graph, k[0])) >= 5 and k[0] in global_apps.app_configs
            }
        )
        for app in labels:
            print(f"== {app}")
            for start, end, count, blocked in cut_points(app):
                print(
                    f"  {count:3d} migrations  {start} .. {end}   blocked by: {blocked or '-'}"
                )
        return 0
    if args.command == "app":
        print(squash_range(args.app, None, None))
        return 0
    if args.command == "range":
        print(squash_range(args.app, args.start, args.end))
        return 0
    if args.command == "regen":
        mdir = Path(global_apps.get_app_config(args.app).path) / "migrations"
        replaced = _replaces_of(mdir / f"{args.squash_name}.py")
        if not replaced:
            parser.error("not a replaces-squash")
        print(
            squash_range(
                args.app, replaced[0], _leaf(originals_graph()[1], args.app, replaced)
            )
        )
        return 0
    if args.command == "snapshot-db":
        print("\n".join(snapshot_db(args.database)))
        return 0
    if args.command == "compare-schema":
        return compare_schema(args.before, args.after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
