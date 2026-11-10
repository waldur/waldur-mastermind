"""Every value put into an event message template is escaped.

``event_logger.emit`` treats its message as a template: ``render_message``
fills a bare ``{name}`` from the event context and turns ``{{`` into ``{``.
Callers build that template with f-strings, ``%``, ``+`` or ``str.format``,
and many of the values they put in are names, descriptions and reasons that
people write. Unless such a value goes through ``event_logger.literal()``,
``AA{{BB}}`` shows as ``AA{BB}`` and a ``{name}`` in it is filled from the
context. This test reads the source of every emitter and lists the values that
go into a template unescaped.

Accepted as they are: ``literal(...)``, a constant, and a translation of a
constant. Accepted when every part is: an f-string, ``%``, ``+``,
``str.format``, ``str.replace`` (the text and its replacement), ``strip`` and
its kin, ``and``/``or``, a conditional expression, an item of a dict written
out in place, and a call to one of ``REWORDINGS``, which reword a template
without putting text into it. A conditional such as
``" {user_full_name}" if x else ""`` is how an emitter adds a placeholder of
its own, so it is accepted as written.

A local name is followed to its assignments (``=``, annotated, ``+=``) in the
same function, and each of them must be accepted. A name with no such
assignment, such as a parameter, a loop variable or a global, cannot be
followed and is not checked: a template passed in from elsewhere is left to
the code that builds it.
"""

import ast
import pathlib
from collections import defaultdict

from django.test import SimpleTestCase

SRC = pathlib.Path(__file__).resolve().parents[3]

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

TRANSLATIONS = ("_", "gettext", "gettext_lazy")

TRIMS = ("strip", "lstrip", "rstrip", "removeprefix", "removesuffix")

# Proposal events call a proposal an access request where the deployment does.
REWORDINGS = ("worded",)


def is_call_to(node, *names):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in names
    return isinstance(func, ast.Name) and func.id in names


def is_method_call(node, *names):
    return is_call_to(node, *names) and isinstance(node.func, ast.Attribute)


def is_emit(node):
    if is_call_to(node, "emit_failed_login"):
        return True
    return is_method_call(node, "emit") and "event_logger" in ast.unparse(
        node.func.value
    )


def template_of(call):
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "message_template":
            return keyword.value
    return None


def own_nodes(scope):
    """The nodes of a module or function, without those of the scopes in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, SCOPES):
            stack.extend(ast.iter_child_nodes(node))


class Scope:
    def __init__(self, scope):
        self.nodes = list(own_nodes(scope))
        self.assignments = defaultdict(list)
        for node in self.nodes:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign | ast.AugAssign) and node.value:
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    self.assignments[target.id].append(node.value)

    def unescaped(self, node, followed=frozenset()):
        """The parts of ``node`` that put text into a template unescaped."""
        translated = is_call_to(node, *TRANSLATIONS) and all(
            isinstance(arg, ast.Constant) for arg in node.args
        )
        if isinstance(node, ast.Constant) or is_call_to(node, "literal") or translated:
            return []
        if isinstance(node, ast.Name):
            if node.id in followed:
                return []
            return self.all_unescaped(self.assignments[node.id], followed | {node.id})
        if isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = [node.left, node.right]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if isinstance(node.right, ast.Tuple):
                parts = [node.left, *node.right.elts]
            elif isinstance(node.right, ast.Dict):
                parts = [node.left, *node.right.values]
            else:
                parts = [node.left, node.right]
        elif isinstance(node, ast.IfExp):
            parts = [node.body, node.orelse]
        elif isinstance(node, ast.BoolOp):
            parts = node.values
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Dict):
            parts = node.value.values
        elif is_method_call(node, "format"):
            parts = [node.func.value, *node.args, *(k.value for k in node.keywords)]
        elif is_method_call(node, "replace") and len(node.args) >= 2:
            parts = [node.func.value, node.args[1]]
        elif is_method_call(node, *TRIMS):
            parts = [node.func.value]
        elif is_call_to(node, *REWORDINGS):
            parts = node.args
        else:
            return [node]
        return self.all_unescaped(parts, followed)

    def all_unescaped(self, nodes, followed):
        return [part for node in nodes for part in self.unescaped(node, followed)]


def unescaped_values():
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if {"tests", "migrations"} & set(relative.parts):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for scope_node in [tree, *(n for n in ast.walk(tree) if isinstance(n, SCOPES))]:
            scope = Scope(scope_node)
            for node in scope.nodes:
                template = template_of(node) if is_emit(node) else None
                if template is not None:
                    for value in scope.unescaped(template):
                        yield str(relative), value.lineno, ast.unparse(value)


class EventMessageSourcesTest(SimpleTestCase):
    def test_values_put_into_templates_are_escaped(self):
        found = [
            f"{path}:{line} {value}"
            for path, line, value in sorted(set(unescaped_values()))
        ]
        if found:
            self.fail(
                "These values go into an event message template unescaped. "
                "Wrap each in event_logger.literal(), so that it shows as "
                "written:\n" + "\n".join(found)
            )
