from constance import config
from django.test import TestCase

from waldur_mastermind.support.backend import SupportBackendType, get_active_backend
from waldur_mastermind.support.backend.basic import BasicBackend


class DefaultBackendTest(TestCase):
    # A deployment that never chose a backend must get the built-in service
    # desk, not an external one it has no URL or credentials for. Changing this
    # default switches existing deployments that relied on it, so it needs an
    # upgrade note.
    def test_built_in_service_desk_is_the_default(self):
        self.assertEqual(
            config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE, SupportBackendType.BASIC
        )
        self.assertIsInstance(get_active_backend(), BasicBackend)
