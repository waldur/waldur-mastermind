"""Unit tests for the SCIM filter parser."""

from django.test import SimpleTestCase

from waldur_core.users.scim.server.exceptions import ScimError
from waldur_core.users.scim.server.filters import (
    USER_FILTER_FIELDS,
    parse,
)


class FilterParserTest(SimpleTestCase):
    def _q(self, expression):
        return parse(expression, USER_FILTER_FIELDS)

    def test_eq(self):
        q = self._q('userName eq "alice"')
        self.assertEqual(q.children, [("username__iexact", "alice")])

    def test_ne(self):
        q = self._q('userName ne "alice"')
        self.assertTrue(q.negated)

    def test_co(self):
        q = self._q('userName co "ali"')
        self.assertEqual(q.children, [("username__icontains", "ali")])

    def test_sw(self):
        q = self._q('userName sw "al"')
        self.assertEqual(q.children, [("username__istartswith", "al")])

    def test_ew(self):
        q = self._q('userName ew "ce"')
        self.assertEqual(q.children, [("username__iendswith", "ce")])

    def test_pr(self):
        q = self._q("userName pr")
        # `pr` yields AND of isnull=False and not exact=""
        self.assertEqual(q.connector, "AND")

    def test_and(self):
        q = self._q('userName eq "alice" and emails eq "a@example.com"')
        self.assertEqual(q.connector, "AND")

    def test_or(self):
        q = self._q('userName eq "alice" or userName eq "bob"')
        self.assertEqual(q.connector, "OR")

    def test_parens(self):
        q = self._q('userName eq "alice" and (emails sw "a" or emails sw "b")')
        self.assertEqual(q.connector, "AND")

    def test_not(self):
        q = self._q('not (userName eq "alice")')
        self.assertTrue(q.negated)

    def test_dotted_path(self):
        q = self._q('name.givenName eq "Alice"')
        self.assertEqual(q.children, [("first_name__iexact", "Alice")])

    def test_unknown_attribute_returns_400(self):
        with self.assertRaises(ScimError) as ctx:
            self._q('unknown eq "x"')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.scim_type, "invalidFilter")

    def test_unknown_operator_returns_400(self):
        with self.assertRaises(ScimError) as ctx:
            self._q('userName xx "x"')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.scim_type, "invalidFilter")

    def test_empty_filter_returns_400(self):
        with self.assertRaises(ScimError):
            self._q("")

    def test_missing_close_paren(self):
        with self.assertRaises(ScimError):
            self._q('(userName eq "a"')
