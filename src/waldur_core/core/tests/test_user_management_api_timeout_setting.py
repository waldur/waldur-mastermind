from django.test import SimpleTestCase
from pydantic.v1 import ValidationError

from waldur_core.core.metadata import WaldurCore


class UserManagementApiTimeoutSettingTest(SimpleTestCase):
    """USER_MANAGEMENT_API_TIMEOUT is passed to httpx as a per-operation timeout.

    Some of the calls it governs run inside API requests, under gunicorn's
    30-second worker timeout, so it is bounded to (0, 30].
    """

    def test_default_is_five_seconds(self):
        self.assertEqual(WaldurCore().USER_MANAGEMENT_API_TIMEOUT, 5)

    def test_values_in_range_are_accepted(self):
        for value in (0.5, 5, 30):
            with self.subTest(value=value):
                config = WaldurCore(USER_MANAGEMENT_API_TIMEOUT=value)
                self.assertEqual(config.USER_MANAGEMENT_API_TIMEOUT, value)

    def test_values_out_of_range_are_rejected(self):
        for value in (0, -1, 30.5, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    WaldurCore(USER_MANAGEMENT_API_TIMEOUT=value)
