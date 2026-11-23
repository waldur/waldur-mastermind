import io
import re
from contextlib import redirect_stdout

from django.core.management import call_command
from django.test import SimpleTestCase

from waldur_core.core.management.commands.print_permissions_description import (
    ts_string,
)
from waldur_core.permissions.enums import PERMISSION_LABELS, PermissionEnum

# A single-quoted TypeScript literal: no bare quote or trailing backslash inside.
TS_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'")


class PrintPermissionsDescriptionTest(SimpleTestCase):
    def test_quotes_and_backslashes_are_escaped(self):
        self.assertEqual(ts_string("the offering's agent"), "'the offering\\'s agent'")
        self.assertEqual(ts_string("a\\b"), "'a\\\\b'")
        self.assertEqual(ts_string("a\nb\u2028c"), "'a\\nb\\u2028c'")

    def test_every_label_is_a_valid_literal(self):
        # Homeport compiles this output as PermissionOptions.tsx; a label
        # with an apostrophe used to end its string early.
        self.assertIn(
            "'", PERMISSION_LABELS[PermissionEnum.MANAGE_OFFERING_AGENT.value]
        )
        output = io.StringIO()
        with redirect_stdout(output):
            call_command("print_permissions_description")
        for line in output.getvalue().splitlines():
            match = re.fullmatch(r"\s*label: translate\((.*)\),", line)
            if match:
                self.assertRegex(match.group(1), rf"^{TS_LITERAL.pattern}$", line)
