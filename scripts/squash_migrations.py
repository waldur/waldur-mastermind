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

A ``replaces``-squash is NOT a fresh-database-only artefact. Django applies it
whenever all *or none* of its replaced migrations are applied
(``MigrationLoader.build_graph``), so a deployment upgrading from before the range
runs the squash instead of the originals. Every data operation of the range must
therefore survive in the squash, at the point of the history where it was written:
the regeneration walks the replaced operations in plan order and emits a state diff
for each run of schema operations, keeping ``RunPython``, ``RunSQL``,
``SeparateDatabaseAndState`` (and any other operation the models cannot express)
verbatim in between. Data code is referenced from the original module through the
``_original()`` helper written into the squash; the originals stay in place anyway,
Django needs them for partially-applied databases. ``elidable`` is deliberately
ignored: the flag encodes the same false premise.

Hand-added ``RunSQL`` in an existing squash that no original carries (DDL an original
issued from Python, which ``migrate_fresh`` cannot see) is kept at the end.

Environment: DJANGO_SETTINGS_MODULE (defaults to waldur_core.server.test_settings).
"""

from __future__ import annotations

import argparse
import inspect
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
    RunPython,  # noqa: E402
    RunSQL,  # noqa: E402
)
from django.db.migrations.autodetector import MigrationAutodetector  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402
from django.db.migrations.operations import (  # noqa: E402
    AlterField,
    AlterUniqueTogether,
    DeleteModel,
    RemoveConstraint,
    RemoveField,
    RemoveIndex,
    RenameModel,
    SeparateDatabaseAndState,
)
from django.db.migrations.operations import fields as field_ops  # noqa: E402
from django.db.migrations.operations import models as model_ops  # noqa: E402
from django.db.migrations.questioner import MigrationQuestioner  # noqa: E402
from django.db.migrations.serializer import BaseSerializer  # noqa: E402
from django.db.migrations.state import ProjectState  # noqa: E402
from django.db.migrations.writer import MigrationWriter  # noqa: E402

from waldur_core.core.management.commands.migrate_fresh import iter_sql  # noqa: E402
from waldur_core.core.migration_operations import FlushDeferredSql  # noqa: E402

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


class _OriginalRef:
    """A callable of a replaced migration, written as ``_original("<module>").<name>``.

    Django's writer would emit ``import app.migrations.0255_x``, which is not valid
    Python; the squash instead imports the module by name at load time.
    """

    def __init__(self, expression: str, imports: set[str], func):
        self.expression, self.imports, self.func = expression, imports, func

    def __call__(self, *args, **kwargs):  # RunPython insists on a callable
        return self.func(*args, **kwargs)


class _OriginalRefSerializer(BaseSerializer):
    def serialize(self):
        return self.value.expression, set(self.value.imports)


MigrationWriter.register_serializer(_OriginalRef, _OriginalRefSerializer)

ORIGINAL_HELPER = '''

def _original(name):
    """A replaced migration's module: its data code runs from there, unchanged."""
    return import_module(__name__.rpartition(".")[0] + "." + name)
'''


def _ref(func, package: str):
    """Rewrite a RunPython callable so the squash can be written to disk."""
    if func is None:
        return None
    if func is RunPython.noop:
        return _OriginalRef(
            "migrations.RunPython.noop", {"from django.db import migrations"}, func
        )
    module = getattr(func, "__module__", "") or ""
    if not module.startswith(package + "."):
        return func  # importable from elsewhere; Django serializes it itself
    name, qualname = module[len(package) + 1 :], func.__qualname__
    if "<" in qualname or "." in name:
        raise RuntimeError(
            f"{module}.{qualname} cannot be referenced from a squash; "
            "make it a module-level function"
        )
    return _OriginalRef(
        f'_original("{name}").{qualname}',
        {"from importlib import import_module"},
        func,
    )


def _rewrite(op, package: str):
    if isinstance(op, RunPython):
        return RunPython(
            _ref(op.code, package),
            _ref(op.reverse_code, package),
            atomic=op.atomic,
            hints=op.hints,
            elidable=op.elidable,
        )
    if isinstance(op, SeparateDatabaseAndState):
        return SeparateDatabaseAndState(
            database_operations=[_rewrite(o, package) for o in op.database_operations],
            state_operations=list(op.state_operations),
        )
    return op


def _is_schema_op(op) -> bool:
    """Operations the autodetector can regenerate from a state diff."""
    return type(op).__module__ in (model_ops.__name__, field_ops.__name__)


def _sql_statements(op: RunSQL) -> list[str]:
    return [re.sub(r"\s+", " ", statement).strip() for statement in iter_sql(op.sql)]


def _fix_together_order(app: str, ops: list, from_state: ProjectState) -> list:
    """Put an AlterUniqueTogether back in front of the RemoveField it must precede.

    For models deleted together the autodetector emits AlterUniqueTogether(None)
    before the RemoveField of each relation, but the RemoveField's dependency only
    names RemoveIndex/RemoveConstraint, so the topological sort may swap them and
    the database step then fails on the vanished field.
    """
    for op in list(ops):
        if not isinstance(op, AlterUniqueTogether):
            continue
        model = from_state.models.get((app, op.name_lower))
        if model is None:
            continue
        covered = {
            field
            for fields in model.options.get("unique_together") or ()
            for field in fields
        }
        for j in range(ops.index(op)):
            earlier = ops[j]
            if (
                isinstance(earlier, RemoveField)
                and earlier.model_name_lower == op.name_lower
                and earlier.name_lower in covered
            ):
                ops.insert(j, ops.pop(ops.index(op)))
                break
    return ops


def _diff(app: str, from_state: ProjectState, to_state: ProjectState) -> list:
    _normalize(from_state)
    _normalize(to_state)
    # The autodetector renders both states; give it clones so the walked state
    # stays unrendered (rendered states pay a model reload on every operation).
    changes = MigrationAutodetector(
        from_state.clone(), to_state.clone(), questioner=_KeepRenames()
    )._detect_changes()
    ops = [op for m in changes.get(app, []) for op in m.operations]
    return _fix_together_order(app, _drop_teardown_of_deleted_models(ops), from_state)


def _drop_teardown_of_deleted_models(ops: list) -> list:
    """Delete a model with DeleteModel alone, as the originals do.

    Before a DeleteModel the autodetector drops the model's unique_together,
    indexes, constraints and relation fields one by one. Each of those steps
    introspects the database for the object it removes and fails when the history
    never created it - a CreateModel whose table check skipped the DDL, a
    constraint added to the state only. DROP TABLE ... CASCADE removes them all,
    which is what a hand-written DeleteModel relied on.
    """
    deleted = {op.name_lower for op in ops if isinstance(op, DeleteModel)}
    teardown = (AlterUniqueTogether, RemoveIndex, RemoveConstraint, RemoveField)
    return [
        op
        for op in ops
        if not (
            isinstance(op, teardown)
            and getattr(op, "model_name_lower", getattr(op, "name_lower", None))
            in deleted
        )
    ]


GET_MODEL_RE = re.compile(r"""get_model\(\s*["'](\w+)[."']""")


def _data_dependencies(app: str, ops: list, graph, present: set) -> list:
    """Dependencies that put every app a kept RunPython reads into its state.

    A RunPython sees the models of every migration applied *before it in the plan*,
    not only of its declared dependencies, so an original could read another app's
    model without depending on it and get away with it by plan order. A squash
    sits elsewhere in the plan. The walk assumed the other apps at their latest
    non-descendant migration (``present``); depend on exactly those leaves for each
    app named in a ``get_model()`` call of the kept code's modules.
    """
    referenced: set[str] = set()
    for op in ops:
        for func in (getattr(op, "code", None), getattr(op, "reverse_code", None)):
            func = getattr(func, "func", func)  # unwrap _OriginalRef
            module = sys.modules.get(getattr(func, "__module__", None) or "")
            if module is None or func is RunPython.noop:
                continue
            referenced.update(GET_MODEL_RE.findall(inspect.getsource(module)))
    dependencies = []
    for label in sorted(referenced - {app}):
        nodes = {n for n in present if n[0] == label}
        parents = set().union(
            *({p.key for p in graph.node_map[n].parents} for n in nodes)
        )
        dependencies += sorted(nodes - parents)
    return dependencies


def _changes_column_type(app: str, ops: list, from_state: ProjectState) -> bool:
    """Whether a diff alters a field to a different field class."""
    for op in ops:
        if not isinstance(op, AlterField):
            continue
        model = from_state.models.get((app, op.model_name_lower))
        old = model.fields.get(op.name) if model else None
        if old is not None and old.deconstruct()[1] != op.field.deconstruct()[1]:
            return True
    return False


def _segment(app: str, from_state, to_state, originals: list) -> list:
    """The schema operations of one segment.

    Normally the autodetector diff. When that diff would change a column's type,
    the originals' own operations are used instead: a rename + add + copy + remove
    sequence around a data step collapses into an AlterField whose cast the
    database may refuse (jsonb to inet), and only the originals know the safe way.
    """
    ops = _diff(app, from_state, to_state)
    if _changes_column_type(app, ops, from_state):
        ops = list(originals)
    return ops


def _flushed(ops: list) -> list:
    """``ops`` followed by a deferred-DDL flush, unless one is already last.

    Tables created earlier in the squash still have their indexes and unique/FK
    constraints queued as deferred SQL. Every kept operation runs after a flush,
    which is what the original chain's migration boundary gave it: data code sees
    the constraints, and a later segment can alter or drop them.
    """
    if ops and not isinstance(ops[-1], FlushDeferredSql):
        ops.append(FlushDeferredSql())
    return ops


def regenerate(app: str, squash_name: str, end: str, to_models: bool) -> int:
    """Rewrite ``squash_name``'s operations from the originals it replaces.

    States are built from original migrations:
      from = all nodes - range - descendants(range)
      to   = all nodes - descendants(range)   (or the real models when ``to_models``)

    The range's operations are walked in plan order. Schema operations only advance
    the state; every other operation (RunPython, RunSQL, SeparateDatabaseAndState,
    a RenameModel of a pre-existing model, ...) is emitted verbatim, preceded by the
    autodetector diff of the schema operations since the previous one. The result is
    a squash that runs the same data code against the same historical state as the
    originals - which is what an upgrading database needs - with far fewer schema
    operations. Dependencies and ``replaces`` of the existing squash file are kept.
    """
    full = MigrationLoader(None)
    existing = full.get_migration(app, squash_name)
    loader, graph = originals_graph()
    package = MigrationLoader.migrations_module(app)[0]
    # The range is what the squash replaces, not a name interval: a five-digit
    # name such as 00012_x sorts before 0001_initial.
    rng = {key for key in existing.replaces if key in graph.nodes}
    descendants = set().union(*(set(graph.backwards_plan(n)) for n in rng)) - rng
    everything = set(graph.nodes)
    order = [key for key in graph.forwards_plan((app, end)) if key in rng]
    if set(order) != rng:
        raise RuntimeError(
            f"{end} does not depend on every migration of the range: "
            f"{sorted(n[1] for n in rng - set(order))}"
        )

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

    # The squash is one transaction. A kept RunPython from an ``atomic = False``
    # original (a batch-committing backfill) still works, it only loses its
    # resumability; DDL that cannot run inside a transaction cannot be squashed.
    for key in order:
        migration = graph.nodes[key]
        if not migration.atomic:
            for op in migration.operations:
                if isinstance(op, RunSQL) and any(
                    "CONCURRENTLY" in statement.upper()
                    for statement in iter_sql(op.sql)
                ):
                    raise RuntimeError(
                        f"{key[1]} runs non-transactional DDL and cannot be squashed"
                    )

    walked = from_state.clone()
    segment_start = from_state
    ops: list = []
    segment_ops: list = []  # the originals' schema operations since the last kept op
    kept_sql: set[str] = set()
    for key in order:
        for op in graph.nodes[key].operations:
            keep = not _is_schema_op(op)
            if isinstance(op, RunPython) and op.code is RunPython.noop:
                keep = False  # a pure no-op only costs a state render
            if isinstance(op, RenameModel):
                # A RenameModel also rewrites FKs in *other* apps' state, which a
                # Delete+Create diff cannot reproduce. Models created inside the
                # segment are simply created under their final name instead.
                keep = (app, op.old_name_lower) in segment_start.models
            if keep:
                ops += _segment(app, segment_start, walked, segment_ops)
                _flushed(ops)
                ops.append(_rewrite(op, package))
                if isinstance(op, RunSQL):
                    kept_sql.update(_sql_statements(op))
                op.state_forwards(app, walked)
                segment_start = walked.clone()
                segment_ops = []
            else:
                op.state_forwards(app, walked)
                segment_ops.append(op)
    ops += _segment(app, segment_start, to_state, segment_ops)

    # Hand-added DDL in the existing squash that no original carries as RunSQL
    # (an original may have issued it from Python; migrate_fresh only sees RunSQL).
    for op in existing.operations:
        if isinstance(op, RunSQL):
            extra = [s for s in _sql_statements(op) if s not in kept_sql]
            if extra:
                ops.append(RunSQL(extra))
                kept_sql.update(extra)

    # A dependency must not point into anything that depends on this squash. At
    # runtime other squashes are single nodes, so take the descendants there and
    # expand each squash among them to the originals it replaces.
    runtime_descendants = set()
    for node in set(full.graph.backwards_plan((app, squash_name))) - {
        (app, squash_name)
    }:
        runtime_descendants.add(node)
        runtime_descendants.update(full.graph.nodes[node].replaces)
    dependencies = list(existing.dependencies)
    for dep in _data_dependencies(
        app, ops, graph, everything - rng - descendants - runtime_descendants
    ):
        if dep not in dependencies:
            dependencies.append(dep)

    new = type(
        "Migration",
        (Migration,),
        {
            "operations": ops,
            "dependencies": dependencies,
            "replaces": existing.replaces,
            "initial": existing.initial,
        },
    )(squash_name, app)
    text = MigrationWriter(new).as_string()
    # Django writes anything else that lives in a migration module (a custom
    # Operation class, a callable used as a field argument) as
    # ``app.migrations.0002_x.Name`` plus an ``import`` line that cannot parse, and
    # asks for manual porting. Route those through _original() like RunPython code.
    text = re.sub(
        r"\b" + re.escape(package) + r"\.(\d\w*)\.", r'_original("\1").', text
    )
    text = re.sub(
        r"\n\n# Functions from the following migrations need manual copying\.\n(#.*\n)+",
        "\n",
        text,
    )
    if "_original(" in text:
        if "from importlib import import_module" not in text:
            text = text.replace(
                "from django.db import migrations",
                "from importlib import import_module\nfrom django.db import migrations",
                1,
            )
        marker = "\n\nclass Migration(migrations.Migration):"
        text = text.replace(marker, ORIGINAL_HELPER + marker, 1)
    Path(MigrationWriter(existing).path).write_text(text)
    return len(ops)


def strip_run_ops(path: Path) -> int:
    """Make a freshly generated Django squash importable.

    ``squashmigrations`` writes RunPython callables as ``app.migrations.0002_x.func``,
    which does not parse. regenerate() rewrites every operation from the originals
    anyway and only needs the file for its dependencies/replaces, so drop them here.
    Done on the AST so string literals containing parentheses cannot confuse it.
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
        ops = regenerate(app, name, end, to_models=is_leaf)
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


# Tables whose rows are not a product of the schema. constance rows appear lazily
# on any config read; the others are seeded by data migrations on `migrate` and by
# import_roles / load_notifications on every deployment path afterwards.
SEEDED_TABLES = (
    "django_migrations",
    "constance_constance",
    "core_notification",
    "permissions_role",
    "permissions_rolepermission",
)


def snapshot_db(alias: str = "default") -> list[str]:
    """Normalized description of the connected database's schema, one line per fact.

    Auto-generated constraint/index names are dropped (they depend on migration
    history), everything structural is kept: columns, types, nullability, defaults,
    index definitions, constraint definitions, triggers, functions, and the row count
    of every table (rows created by post_migrate receivers must match too) except
    those that data migrations or the seed commands fill (SEEDED_TABLES).
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
            if table in SEEDED_TABLES:
                continue
            if table == "django_content_type":
                # Data migrations leave content types of since-removed models
                # behind (get_or_create while remapping scopes); only the live
                # ones are a post_migrate product both paths must agree on.
                cursor.execute("SELECT app_label, model FROM django_content_type")
                live = sum(
                    1
                    for app_label, model in cursor.fetchall()
                    if app_label in global_apps.app_configs
                    and model in global_apps.app_configs[app_label].models
                )
                lines.append(f"rows {table} {live} (live models)")
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
