from django.conf import settings
from rest_framework import test

from . import fixtures


class BaseTest(test.APITransactionTestCase):

    def setUp(self):
        support_backend = 'nodeconductor_assembly_waldur.support.backend.atlassian:SupportBackend'
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND'] = support_backend
        self.fixture = fixtures.SupportFixture()
