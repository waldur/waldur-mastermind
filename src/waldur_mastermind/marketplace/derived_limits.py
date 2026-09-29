"""Component limits derived from order-form options.

Two option types let an offering compute limits instead of asking for them:

- ``component_formula`` takes one number from the customer and sets each of
  its target components to a formula over that number;
- ``component_sum`` sets its target component to the sum of other limit
  components, which may themselves be derived.

The server owns every derived limit: whatever a client sends for one is
replaced when an order is created, so a crafted request cannot price a
different quantity than the form shows.

Formulas use a deliberately small grammar -- ``input``, numbers, ``+ - * /``
and parentheses -- parsed by hand rather than with ``eval`` or ``ast``, so the
order form can implement exactly the same language.

Arithmetic is exact (rational numbers), not Decimal: Decimal division rounds
at 28 digits, so ``input * 2 / 3 * 3`` would come out a hair above ``2 * input``
and round up to the next whole unit. The order form uses exact fractions too,
so both sides agree on every result.
"""

import math
import re
from fractions import Fraction

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

FORMULA_TYPE = "component_formula"
SUM_TYPE = "component_sum"
DERIVED_OPTION_TYPES = (FORMULA_TYPE, SUM_TYPE)

MAX_FORMULA_LENGTH = 255
# Nesting depth of parentheses and unary signs; keeps the recursive parser
# far from Python's recursion limit whatever a provider types.
MAX_FORMULA_DEPTH = 32

# ASCII digits only: Python's \d would also take other scripts' digits, which
# the order form's evaluator does not.
_TOKEN_RE = re.compile(r"\s*(?:([0-9]+(?:\.[0-9]+)?|\.[0-9]+)|(input)\b|([-+*/()]))")


class FormulaError(ValueError):
    pass


def _tokenize(text):
    tokens = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if not match:
            raise FormulaError(
                _("Unexpected character %(char)r at position %(position)s.")
                % {"char": text[position:].lstrip()[:1], "position": position + 1}
            )
        number, name, operator = match.groups()
        if number is not None:
            tokens.append(("num", Fraction(number)))
        elif name is not None:
            tokens.append(("input", None))
        else:
            tokens.append((operator, None))
        position = match.end()
    return tokens


class _Parser:
    """Recursive descent over expr := term (('+'|'-') term)*,
    term := unary (('*'|'/') unary)*, unary := ('+'|'-') unary | primary,
    primary := number | 'input' | '(' expr ')'."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.position = 0
        self.depth = 0

    def peek(self):
        if self.position < len(self.tokens):
            return self.tokens[self.position][0]
        return None

    def take(self):
        token = self.tokens[self.position]
        self.position += 1
        return token

    def parse(self):
        if not self.tokens:
            raise FormulaError(_("Formula is empty."))
        node = self.expr()
        if self.peek() is not None:
            raise FormulaError(_("Unexpected %(token)r.") % {"token": self.peek()})
        return node

    def expr(self):
        node = self.term()
        while self.peek() in ("+", "-"):
            operator = self.take()[0]
            node = (operator, node, self.term())
        return node

    def term(self):
        node = self.unary()
        while self.peek() in ("*", "/"):
            operator = self.take()[0]
            node = (operator, node, self.unary())
        return node

    def unary(self):
        if self.peek() in ("+", "-"):
            operator = self.take()[0]
            node = self.nested(self.unary)
            return ("neg", node) if operator == "-" else node
        return self.primary()

    def primary(self):
        kind = self.peek()
        if kind == "num":
            return ("num", self.take()[1])
        if kind == "input":
            self.take()
            return ("input",)
        if kind == "(":
            self.take()
            node = self.nested(self.expr)
            if self.peek() != ")":
                raise FormulaError(_("Missing closing parenthesis."))
            self.take()
            return node
        if kind is None:
            raise FormulaError(_("Formula ends unexpectedly."))
        raise FormulaError(_("Unexpected %(token)r.") % {"token": kind})

    def nested(self, rule):
        self.depth += 1
        if self.depth > MAX_FORMULA_DEPTH:
            raise FormulaError(_("Formula is nested too deeply."))
        try:
            return rule()
        finally:
            self.depth -= 1


def parse_formula(text):
    """Parse a formula into a tree, raising FormulaError when it is not one."""
    if not isinstance(text, str):
        raise FormulaError(_("Formula must be a string."))
    if len(text) > MAX_FORMULA_LENGTH:
        raise FormulaError(
            _("Formula is longer than %(max)s characters.")
            % {"max": MAX_FORMULA_LENGTH}
        )
    return _Parser(_tokenize(text)).parse()


def evaluate_formula(node, value):
    """Evaluate a parsed formula with ``input`` bound to ``value``, exactly."""
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "input":
        return value
    if kind == "neg":
        return -evaluate_formula(node[1], value)
    left = evaluate_formula(node[1], value)
    right = evaluate_formula(node[2], value)
    if kind == "+":
        return left + right
    if kind == "-":
        return left - right
    if kind == "*":
        return left * right
    if right == 0:
        raise FormulaError(_("Division by zero."))
    return left / right


def _config(option, key):
    return option.get(key) or {}


def _formula_targets(option):
    return [
        target["component_type"]
        for target in _config(option, "component_formula_config").get("targets", [])
    ]


def _sum_config(option):
    config = _config(option, "component_sum_config")
    return config.get("target_component"), list(config.get("components") or [])


def derived_components(options):
    """Map each derived component type to the name of the option deriving it."""
    result = {}
    for name, option in (options or {}).items():
        field_type = option.get("type")
        if field_type == FORMULA_TYPE:
            for component_type in _formula_targets(option):
                result.setdefault(component_type, name)
        elif field_type == SUM_TYPE:
            target, _components = _sum_config(option)
            if target:
                result.setdefault(target, name)
    return result


def pair_resource_options(resource_options, order_options):
    """Check derived resource options against the order options; return them normalised.

    A ``component_formula`` resource option lets the customer change a formula
    input after ordering. It must share its key with a ``component_formula``
    order option, whose formulas are the only definition: the resource option's
    own config is dropped and it takes the order option's bounds, so a changed
    value is checked exactly as an ordered one. Resource options are copied
    from order options of the same key when a resource is created, which is
    how the ordered value reaches the resource. ``component_sum`` has no value
    to change and stays an order option.
    """
    if not resource_options:
        return resource_options
    order_options = order_options or {}
    options = dict(resource_options.get("options") or {})
    for name, option in options.items():
        field_type = option.get("type")
        if field_type == SUM_TYPE:
            raise serializers.ValidationError(
                {
                    "resource_options": _(
                        "Option %s: component_sum is only available as an order option."
                    )
                    % name
                }
            )
        if field_type != FORMULA_TYPE:
            continue
        paired = order_options.get(name) or {}
        if paired.get("type") != FORMULA_TYPE:
            raise serializers.ValidationError(
                {
                    "resource_options": _(
                        "Option %s: a component_formula resource option needs a "
                        "component_formula order option with the same internal "
                        "name, whose formulas it uses."
                    )
                    % name
                }
            )
        option = {
            key: value
            for key, value in option.items()
            if key not in ("component_formula_config", "min", "max")
        }
        for bound in ("min", "max"):
            if paired.get(bound) is not None:
                option[bound] = paired[bound]
        options[name] = option
    return {**resource_options, "options": options}


def paired_resource_options(resource_options, order_options):
    """Names of resource options that change a formula input of an order option."""
    order_options = order_options or {}
    return {
        name
        for name, option in ((resource_options or {}).get("options") or {}).items()
        if option.get("type") == FORMULA_TYPE
        and (order_options.get(name) or {}).get("type") == FORMULA_TYPE
    }


def referenced_components(options):
    """Map each component a derived option reads or sets to that option's name."""
    result = {}
    for name, option in (options or {}).items():
        field_type = option.get("type")
        if field_type == FORMULA_TYPE:
            referenced = _formula_targets(option)
        elif field_type == SUM_TYPE:
            target, components = _sum_config(option)
            referenced = [target, *components]
        else:
            continue
        for component_type in referenced:
            result.setdefault(component_type, name)
    return result


def _sum_order(options):
    """(target, components) of each sum, after the sums it reads."""
    sums = {}
    for name, option in options.items():
        if option.get("type") == SUM_TYPE:
            target, components = _sum_config(option)
            sums[target] = (name, components)

    ordered = []
    state = {}

    def visit(target, path):
        if state.get(target) == "done":
            return
        if state.get(target) == "visiting":
            cycle = " -> ".join([*path, target])
            raise serializers.ValidationError(
                {"options": _("Component sums form a cycle: %s.") % cycle}
            )
        state[target] = "visiting"
        for component in sums[target][1]:
            if component in sums:
                visit(component, [*path, target])
        state[target] = "done"
        ordered.append((target, sums[target][1]))

    for target in sums:
        visit(target, [])
    return ordered


def validate_derived_options(options):
    """Check derived options against each other when an offering is saved."""
    owners = {}
    for name, option in (options or {}).items():
        field_type = option.get("type")
        if field_type == FORMULA_TYPE:
            targets = _formula_targets(option)
        elif field_type == SUM_TYPE:
            target, components = _sum_config(option)
            if target in components:
                raise serializers.ValidationError(
                    {
                        "options": _(
                            "Option %(name)s sums component %(component)s into itself."
                        )
                        % {"name": name, "component": target}
                    }
                )
            targets = [target]
        else:
            continue
        for component_type in targets:
            if component_type in owners:
                raise serializers.ValidationError(
                    {
                        "options": _(
                            "Component %(component)s is derived by more than one "
                            "option (%(first)s, %(second)s)."
                        )
                        % {
                            "component": component_type,
                            "first": owners[component_type],
                            "second": name,
                        }
                    }
                )
            owners[component_type] = name
    _sum_order(options or {})


def validate_derived_components(options, limit_types):
    """Check every component a derived option names is a limit component."""
    for name, option in (options or {}).items():
        field_type = option.get("type")
        if field_type == FORMULA_TYPE:
            referenced = _formula_targets(option)
        elif field_type == SUM_TYPE:
            target, components = _sum_config(option)
            referenced = [target, *components]
        else:
            continue
        for component_type in referenced:
            if component_type not in limit_types:
                raise serializers.ValidationError(
                    {
                        "options": _(
                            "Option %(name)s refers to %(component)s, which is not "
                            "a limit-based component of this offering."
                        )
                        % {"name": name, "component": component_type}
                    }
                )


def _round_up(value, component):
    places = getattr(component, "limit_decimal_places", 0) or 0
    scale = 10**places
    return Fraction(math.ceil(value * scale), scale)


def _narrow(value):
    if value.denominator == 1:
        return value.numerator
    return float(value)


def _formula_input(name, option, attributes):
    """The value entered for a formula option, as an exact number, or None.

    Checked here rather than trusted from option validation, because not every
    path that changes an order's attributes runs it (editing a pending order,
    provider approval); a value that is not a number would otherwise surface as
    an unhandled error, and one outside the bounds would derive limits the
    order form never offers.
    """
    value = attributes.get(name)
    if value is None or value == "":
        value = option.get("default")
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Fraction(str(value).strip())
    except (TypeError, ValueError, ZeroDivisionError):
        raise serializers.ValidationError({name: _("A number is required.")})
    if option.get("min") is not None and number < option["min"]:
        raise serializers.ValidationError(
            {
                name: _("Ensure this value is greater than or equal to %s.")
                % option["min"]
            }
        )
    if option.get("max") is not None and number > option["max"]:
        raise serializers.ValidationError(
            {name: _("Ensure this value is less than or equal to %s.") % option["max"]}
        )
    return number


def compute_derived_limits(options, attributes, limits, components, fallback=None):
    """Return ``limits`` with every derived component set by the server.

    ``components`` maps each limit component type to its component; a derived
    component missing from it (deleted, or no longer limit-billed since the
    option was saved) is an offering misconfiguration and refuses the order.
    Client-supplied values for derived components are dropped first. A formula
    whose input is absent (optional, hidden, never recorded) derives nothing,
    and a sum is written only when at least one of its components has a value.
    ``fallback`` supplies the value to keep for a derived component that cannot
    be calculated: an existing resource's current limits, so that a resource
    ordered before the option existed does not lose them on its next update.
    """
    options = options or {}
    derived = derived_components(options)
    if not derived:
        return limits
    for component_type, name in derived.items():
        if component_type not in components:
            raise serializers.ValidationError(
                {
                    "limits": _(
                        "Option %(name)s refers to %(component)s, which is not a "
                        "limit-based component of this plan. The offering needs "
                        "to be corrected by its provider."
                    )
                    % {"name": name, "component": component_type}
                }
            )
    attributes = attributes or {}
    result = {key: value for key, value in (limits or {}).items() if key not in derived}
    exact = {
        key: Fraction(str(value))
        for key, value in result.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }

    for name, option in options.items():
        if option.get("type") != FORMULA_TYPE:
            continue
        value = _formula_input(name, option, attributes)
        if value is None:
            continue
        for target in _config(option, "component_formula_config").get("targets", []):
            component_type = target["component_type"]
            try:
                amount = evaluate_formula(parse_formula(target["formula"]), value)
            except FormulaError as e:
                raise serializers.ValidationError(
                    {
                        name: _("Cannot calculate %(component)s: %(error)s")
                        % {"component": component_type, "error": e}
                    }
                )
            if amount < 0:
                raise serializers.ValidationError(
                    {
                        name: _("Calculated %(component)s is negative.")
                        % {"component": component_type}
                    }
                )
            exact[component_type] = _round_up(amount, components.get(component_type))

    # Before the sums, so that a sum over a kept value adds it in.
    for component_type in derived:
        if component_type not in exact and component_type in (fallback or {}):
            exact[component_type] = Fraction(str(fallback[component_type]))

    for target, sources in _sum_order(options):
        present = [exact[source] for source in sources if source in exact]
        if present:
            exact[target] = _round_up(sum(present), components.get(target))

    for component_type in derived:
        if component_type in exact:
            result[component_type] = _narrow(exact[component_type])
    return result
