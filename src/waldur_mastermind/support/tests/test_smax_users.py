from unittest import mock

import pytest
from rest_framework import test

from waldur_mastermind.support.backend.smax_utils import (
    SmaxBackend,
    SmaxBackendError,
    User,
)


@pytest.mark.override_config(
    SMAX_API_URL="http://localhost:8080",
    SMAX_TENANT_ID="123456789",
)
class SmaxAddUserTest(test.APITestCase):
    def setUp(self):
        self.backend = SmaxBackend()

    def test_missing_last_name_is_rejected_with_identity(self):
        user = User(email="john@example.com", name="John")

        with self.assertRaises(SmaxBackendError) as ctx:
            self.backend.add_user(user)

        self.assertIn("first or last", str(ctx.exception))
        self.assertIn("john@example.com", str(ctx.exception))

    def test_empty_email_is_rejected_up_front(self):
        # The production failure: the system robot has a name but no email, so
        # the Person can never be found again and creation looks opaque.
        user = User(email="", name="System Robot", upn="robot")

        with self.assertRaises(SmaxBackendError) as ctx:
            self.backend.add_user(user)

        self.assertIn("no email address", str(ctx.exception))
        self.assertIn("System Robot", str(ctx.exception))
        # No HTTP request is attempted for an emailless user.

    def test_creation_failure_reports_status_and_response(self):
        response = mock.Mock(status_code=500, text='{"error":"boom"}')
        with (
            mock.patch.object(self.backend, "post", return_value=response),
            mock.patch.object(self.backend, "wait_result", return_value=None),
        ):
            user = User(email="jane@example.com", name="Jane Doe")

            with self.assertRaises(SmaxBackendError) as ctx:
                self.backend.add_user(user)

        message = str(ctx.exception)
        self.assertIn("jane@example.com", message)
        self.assertIn("500", message)
        self.assertIn("boom", message)

    def test_successful_creation_returns_backend_user(self):
        response = mock.Mock(status_code=200, text="{}")
        backend_user = User(email="jane@example.com", name="Jane Doe", id=42)
        with (
            mock.patch.object(self.backend, "post", return_value=response),
            mock.patch.object(self.backend, "wait_result", return_value=backend_user),
        ):
            user = User(email="jane@example.com", name="Jane Doe")
            result = self.backend.add_user(user)

        self.assertEqual(result, backend_user)
