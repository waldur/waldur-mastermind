"""Safe arithmetic formula evaluator for staff-configured pricing expressions.

Used by the volume-discount formulas on plan components (``usage`` bound to
the billed quantity). Formulas are entered by staff but still evaluated inside
the month-close billing pipeline, so evaluation must be side-effect free and
must never execute arbitrary code: the evaluator walks a whitelisted AST
instead of calling ``eval()``.

Supported grammar:

- numbers, the variables passed by the caller (e.g. ``usage``)
- ``+ - * / // % **`` and unary minus
- comparisons and ``and`` / ``or`` / conditional expressions
  (``rate if usage >= 100 else 0``)
- functions: ``MIN, MAX, LN, LOG10, FLOOR, CEIL, POW, ABS``
  (case-insensitive)

``LN`` is the natural logarithm and ``LOG10`` the decimal one. There is
deliberately no plain ``LOG``: spreadsheet dialects disagree on its base,
and a silent base mismatch produces plausible-but-wrong prices.
"""

import ast
import math
from decimal import Decimal, InvalidOperation

MAX_FORMULA_LENGTH = 1000
MAX_NODE_COUNT = 200

FUNCTIONS = {
    "MIN": min,
    "MAX": max,
    "LN": math.log,
    "LOG10": math.log10,
    "FLOOR": math.floor,
    "CEIL": math.ceil,
    "POW": math.pow,
    "ABS": abs,
}

_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}

_COMPARE_OPS = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


class FormulaError(Exception):
    """Raised when a formula cannot be parsed or evaluated."""


def _evaluate_node(node, variables):
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, variables)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise FormulaError(f"Unsupported constant: {node.value!r}")
        return float(node.value)

    if isinstance(node, ast.Name):
        try:
            return float(variables[node.id])
        except KeyError:
            raise FormulaError(f"Unknown variable: {node.id}")

    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand, variables)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise FormulaError(f"Unsupported unary operator: {type(node.op).__name__}")

    if isinstance(node, ast.BinOp):
        operation = _BIN_OPS.get(type(node.op))
        if operation is None:
            raise FormulaError(f"Unsupported operator: {type(node.op).__name__}")
        return operation(
            _evaluate_node(node.left, variables),
            _evaluate_node(node.right, variables),
        )

    if isinstance(node, ast.Compare):
        left = _evaluate_node(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators):
            operation = _COMPARE_OPS.get(type(op))
            if operation is None:
                raise FormulaError(f"Unsupported comparison: {type(op).__name__}")
            right = _evaluate_node(comparator, variables)
            if not operation(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        values = (_evaluate_node(value, variables) for value in node.values)
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)

    if isinstance(node, ast.IfExp):
        if _evaluate_node(node.test, variables):
            return _evaluate_node(node.body, variables)
        return _evaluate_node(node.orelse, variables)

    if isinstance(node, ast.Call):
        if node.keywords or not isinstance(node.func, ast.Name):
            raise FormulaError("Only plain function calls are supported.")
        function = FUNCTIONS.get(node.func.id.upper())
        if function is None:
            raise FormulaError(f"Unknown function: {node.func.id}")
        arguments = [_evaluate_node(argument, variables) for argument in node.args]
        return function(*arguments)

    raise FormulaError(f"Unsupported expression: {type(node).__name__}")


def evaluate(formula: str, **variables) -> Decimal:
    """Evaluate a formula and return the result as Decimal.

    Raises FormulaError on any parse or evaluation problem (unknown
    name, division by zero, math domain error, non-numeric result).
    """
    if not formula or not formula.strip():
        raise FormulaError("Formula is empty.")
    if len(formula) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"Formula is longer than {MAX_FORMULA_LENGTH} characters.")

    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as error:
        raise FormulaError(f"Invalid syntax: {error.msg}")

    if sum(1 for _ in ast.walk(tree)) > MAX_NODE_COUNT:
        raise FormulaError("Formula is too complex.")

    try:
        result = _evaluate_node(tree, variables)
    except FormulaError:
        raise
    except (ZeroDivisionError, ValueError, OverflowError, TypeError) as error:
        raise FormulaError(f"Evaluation failed: {error}")

    if isinstance(result, bool) or not isinstance(result, int | float):
        raise FormulaError("Formula did not produce a number.")
    if math.isnan(result) or math.isinf(result):
        raise FormulaError("Formula produced a non-finite number.")

    try:
        return Decimal(str(result))
    except InvalidOperation as error:
        raise FormulaError(f"Evaluation failed: {error}")


def validate(formula: str, variable_names: tuple[str, ...]) -> None:
    """Validate a formula at save time: parse it and probe-evaluate it
    over a range of sample values so obviously broken formulas are
    rejected before they reach the billing pipeline.

    Raises FormulaError if any probe fails.
    """
    for sample in (0, 1, 10, 1000, 1000000):
        evaluate(formula, **{name: sample for name in variable_names})
