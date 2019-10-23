from io import StringIO

from django.core.management import call_command
from django.test import TestCase


class CommandsTestCase(TestCase):
    def test_no_missing_migrations(self):

        result = StringIO()
        call_command('makemigrations', dry_run=True, stdout=result)
        result_string = result.getvalue()
        self.assertEqual(result_string, 'No changes detected\n')
