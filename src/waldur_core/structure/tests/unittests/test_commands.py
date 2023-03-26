from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from waldur_core.structure.tests import factories


class DumpUsersCommandTest(TestCase):
    def test_dump_users_command_with_unicode_full_name(self):
        user = factories.UserFactory(full_name='äöur šipākøv')
        output = StringIO()
        try:
            call_command('dumpusers', stdout=output)
        except UnicodeDecodeError as e:
            self.fail(str(e))
        value = output.getvalue()
        if not isinstance(value, str):
            value = value.decode('utf-8')
        self.assertIn(user.full_name, value)
