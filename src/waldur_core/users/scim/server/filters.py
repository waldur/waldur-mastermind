"""SCIM 2.0 filter expression parser (RFC 7644 §3.4.2.2).

Supports the subset relevant for Waldur User/Group listing:

- comparison operators: ``eq``, ``ne``, ``co``, ``sw``, ``ew``, ``pr``
- logical operators: ``and``, ``or``, ``not``
- parenthesised sub-expressions
- attribute paths: bare (``userName``), dotted (``name.givenName``),
  schema-prefixed URNs (``urn:...:User:civilNumber``)

The parser produces a Django ``Q`` object via a small attribute-to-ORM-field
map. Unknown attributes / operators raise ``ScimError(400, ..., "invalidFilter")``.

This deliberately omits ``gt/lt/ge/le`` and value-path expressions
(``emails[type eq "work"]``); IdPs (Okta, Entra ID, Keycloak) we target rarely
send those for User/Group listing. Add them when a real client needs them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db.models import Q

from waldur_core.users.scim.server.exceptions import ScimError

_TOKEN_RE = re.compile(
    r"""
    \s*(
        "(?:\\.|[^"\\])*"      # double-quoted string
      | -?\d+(?:\.\d+)?         # number
      | \(                      # open paren
      | \)                      # close paren
      | (?:[\w\-:]+\.)*[\w\-:]+ # attribute path or word
    )
    """,
    re.VERBOSE,
)

_COMPARISON_OPS = {"eq", "ne", "co", "sw", "ew"}
_LOGICAL_OPS = {"and", "or"}
_PRESENT_OP = "pr"


@dataclass(frozen=True)
class FilterField:
    """Description of a SCIM attribute exposed for filtering.

    ``orm_field`` is the Django ORM field path used to build the ``Q`` object.
    ``case_insensitive`` controls whether string equality uses ``iexact``
    (RFC 7643 §2.1 — strings default to case-insensitive matching).
    """

    orm_field: str
    case_insensitive: bool = True
    boolean: bool = False


def parse(expression: str, fields: dict[str, FilterField]) -> Q:
    """Parse a SCIM filter expression and return a Django ``Q`` object.

    ``fields`` maps SCIM attribute paths (lower-case, dotted form) to the ORM
    field they translate to. Attribute paths not in ``fields`` produce a 400.
    """
    tokens = _tokenize(expression)
    if not tokens:
        raise ScimError(400, "Empty filter expression.", scim_type="invalidFilter")
    parser = _Parser(tokens, fields)
    result = parser.parse_or()
    if parser.peek() is not None:
        raise ScimError(
            400,
            f"Unexpected token {parser.peek()!r} at end of filter.",
            scim_type="invalidFilter",
        )
    return result


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN_RE.match(expression, pos)
        if not match:
            stripped = expression[pos:].strip()
            if not stripped:
                break
            raise ScimError(
                400,
                f"Unable to tokenize filter near {stripped[:20]!r}.",
                scim_type="invalidFilter",
            )
        token = match.group(1)
        pos = match.end()
        if token:
            tokens.append(token)
    return tokens


class _Parser:
    def __init__(self, tokens: list[str], fields: dict[str, FilterField]):
        self.tokens = tokens
        self.pos = 0
        self.fields = fields

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self) -> str:
        if self.pos >= len(self.tokens):
            raise ScimError(
                400, "Unexpected end of filter expression.", scim_type="invalidFilter"
            )
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse_or(self) -> Q:
        left = self.parse_and()
        while self._peek_keyword() == "or":
            self.consume()
            right = self.parse_and()
            left = left | right
        return left

    def parse_and(self) -> Q:
        left = self.parse_not()
        while self._peek_keyword() == "and":
            self.consume()
            right = self.parse_not()
            left = left & right
        return left

    def parse_not(self) -> Q:
        tok = self.peek()
        if tok and tok.lower() == "not":
            self.consume()
            if self.peek() != "(":
                raise ScimError(
                    400,
                    "Expected '(' after 'not'.",
                    scim_type="invalidFilter",
                )
            return ~self.parse_primary()
        return self.parse_primary()

    def parse_primary(self) -> Q:
        token = self.peek()
        if token == "(":
            self.consume()
            inner = self.parse_or()
            if self.peek() != ")":
                raise ScimError(400, "Missing closing ')'.", scim_type="invalidFilter")
            self.consume()
            return inner
        return self.parse_atom()

    def parse_atom(self) -> Q:
        attr_token = self.consume()
        if attr_token in {"(", ")"} or _is_string(attr_token) or _is_number(attr_token):
            raise ScimError(
                400,
                f"Expected attribute path, got {attr_token!r}.",
                scim_type="invalidFilter",
            )
        op = self.consume().lower()
        if op == _PRESENT_OP:
            return self._build_present(attr_token)
        if op not in _COMPARISON_OPS:
            raise ScimError(
                400,
                f"Unsupported operator {op!r}. Allowed: eq, ne, co, sw, ew, pr.",
                scim_type="invalidFilter",
            )
        value_token = self.consume()
        return self._build_comparison(attr_token, op, value_token)

    def _peek_keyword(self) -> str | None:
        tok = self.peek()
        if tok and tok.lower() in _LOGICAL_OPS:
            return tok.lower()
        return None

    def _resolve(self, attr_path: str) -> FilterField:
        key = attr_path.lower()
        if key not in self.fields:
            raise ScimError(
                400,
                f"Unsupported filter attribute {attr_path!r}.",
                scim_type="invalidFilter",
            )
        return self.fields[key]

    def _build_present(self, attr_path: str) -> Q:
        field = self._resolve(attr_path)
        # Present := not null and not empty string. For list / JSON fields we
        # approximate the same with __isnull=False.
        return Q(**{f"{field.orm_field}__isnull": False}) & ~Q(**{field.orm_field: ""})

    def _build_comparison(self, attr_path: str, op: str, value_token: str) -> Q:
        field = self._resolve(attr_path)
        value = _unquote(value_token)
        if field.boolean:
            return self._build_boolean(attr_path, op, value, field)
        lookup_base = field.orm_field
        i = "i" if field.case_insensitive else ""
        if op == "eq":
            return Q(**{f"{lookup_base}__{i}exact": value})
        if op == "ne":
            return ~Q(**{f"{lookup_base}__{i}exact": value})
        if op == "co":
            return Q(**{f"{lookup_base}__{i}contains": value})
        if op == "sw":
            return Q(**{f"{lookup_base}__{i}startswith": value})
        if op == "ew":
            return Q(**{f"{lookup_base}__{i}endswith": value})
        raise ScimError(  # pragma: no cover — guarded by caller
            400, f"Unhandled operator {op!r}.", scim_type="invalidFilter"
        )

    def _build_boolean(
        self, attr_path: str, op: str, value: str, field: FilterField
    ) -> Q:
        """Booleans must reach the ORM as real bools, never strings; Entra ID
        is also known to quote them (``active eq "False"``), so accept both."""
        if op not in {"eq", "ne"}:
            raise ScimError(
                400,
                f"Operator {op!r} is not supported for boolean attribute {attr_path!r}.",
                scim_type="invalidFilter",
            )
        normalized = value.lower() if isinstance(value, str) else value
        if normalized not in {"true", "false"}:
            raise ScimError(
                400,
                f"Boolean attribute {attr_path!r} requires a true/false value.",
                scim_type="invalidFilter",
            )
        q = Q(**{field.orm_field: normalized == "true"})
        return ~q if op == "ne" else q


def _is_string(token: str) -> bool:
    return token.startswith('"') and token.endswith('"')


def _is_number(token: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", token))


def _unquote(token: str) -> str:
    if _is_string(token):
        return bytes(token[1:-1], "utf-8").decode("unicode_escape")
    if token.lower() == "true":
        return "true"
    if token.lower() == "false":
        return "false"
    if token.lower() == "null":
        return ""
    return token


# ---------------------------------------------------------------------------
# Field maps used by the User and Group views.
# ---------------------------------------------------------------------------

USER_FILTER_FIELDS: dict[str, FilterField] = {
    "username": FilterField("username"),
    "name.givenname": FilterField("first_name"),
    "name.familyname": FilterField("last_name"),
    "emails": FilterField("email"),
    "emails.value": FilterField("email"),
    "active": FilterField("is_active", case_insensitive=False, boolean=True),
    "displayname": FilterField("username"),
}

GROUP_FILTER_FIELDS: dict[str, FilterField] = {
    # Groups are virtual; we filter on the synthetic displayName ORM-side by
    # constructing it from scope + role. The view layer handles this rather than
    # the parser, so we expose displayName as a sentinel that returns the input
    # value back to the caller (handled in views).
    "displayname": FilterField("display_name"),
}
