from constance.test import override_config
from django.test import TestCase

from waldur_core.core.service_access import get_service_access_mode, names_calls


class NamesCallsTest(TestCase):
    @override_config(SERVICE_ACCESS_MODE="marketplace")
    def test_marketplace_only_drops_the_call_vocabulary(self):
        self.assertFalse(names_calls())

    @override_config(SERVICE_ACCESS_MODE="both")
    def test_both_keeps_it(self):
        self.assertTrue(names_calls())

    @override_config(SERVICE_ACCESS_MODE="calls")
    def test_calls_only_keeps_it(self):
        self.assertTrue(names_calls())

    @override_config(SERVICE_ACCESS_MODE="")
    def test_an_empty_value_falls_back_to_both(self):
        # A deployment that has not been migrated must keep the vocabulary it
        # has always used rather than silently losing it.
        self.assertEqual(get_service_access_mode(), "both")
        self.assertTrue(names_calls())

    @override_config(SERVICE_ACCESS_MODE="something-new")
    def test_an_unrecognised_value_keeps_the_vocabulary(self):
        # Fail towards the terms that are the domain's own everywhere but
        # marketplace-only mode.
        self.assertTrue(names_calls())
