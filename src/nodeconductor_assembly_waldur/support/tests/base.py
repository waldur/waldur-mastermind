from django.conf import settings
from rest_framework import test

from . import fixtures


class BaseTest(test.APITransactionTestCase):

    def setUp(self):
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND'] = 'SupportBackend'
        self.fixture = fixtures.SupportFixture()
