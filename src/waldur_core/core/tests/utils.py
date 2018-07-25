import unittest

from django.conf import settings
from rest_framework import test


def is_not_postgresql():
    return settings.DATABASES['default']['ENGINE'] != 'django.db.backends.postgresql_psycopg2'


@unittest.skipIf(is_not_postgresql(), 'Only for PostgreSQL')
class PostgreSQLTest(test.APITransactionTestCase):
    pass
