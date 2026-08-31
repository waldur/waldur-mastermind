from smtplib import SMTPAuthenticationError
from unittest import mock

from ddt import data, ddt
from django.test import override_settings
from rest_framework import status, test

from waldur_core.core import email_diagnostics
from waldur_core.core.models import Notification
from waldur_core.logging.models import EmailLog
from waldur_core.structure.tests import factories as structure_factories

SMTP_BACKEND = email_diagnostics.SMTP_BACKEND

WORKING_CONFIG = dict(
    EMAIL_BACKEND=SMTP_BACKEND,
    EMAIL_HOST="smtp.waldur.example.net",
    EMAIL_PORT=587,
    EMAIL_HOST_USER="waldur",
    EMAIL_HOST_PASSWORD="secret",
    EMAIL_USE_TLS=True,
    EMAIL_USE_SSL=False,
    EMAIL_TIMEOUT=10,
    DEFAULT_FROM_EMAIL="noreply@waldur.example.net",
    DEFAULT_REPLY_TO_EMAIL="support@waldur.example.net",
)


def codes(diagnostics):
    return {finding.code for finding in diagnostics.findings}


class EmailConfigTest(test.APITestCase):
    @override_settings(**WORKING_CONFIG)
    def test_password_is_reported_as_a_boolean_and_never_returned(self):
        config = email_diagnostics.get_email_config()
        self.assertTrue(config.has_password)
        self.assertNotIn("secret", str(config))


@ddt
class EmailAuditTest(test.APITestCase):
    def setUp(self):
        Notification.objects.create(key="users.invitation_created", enabled=True)

    @override_settings(**WORKING_CONFIG)
    def test_healthy_configuration_reports_no_problem(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.OK, diagnostics.status)

    @override_settings(**{**WORKING_CONFIG, "EMAIL_HOST": ""})
    def test_missing_host_is_an_error(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.ERROR, diagnostics.status)
        self.assertIn("host_missing", codes(diagnostics))

    @data("waldur-smtp", "smtp.example.com")
    def test_placeholder_host_is_an_error(self, host):
        with override_settings(**{**WORKING_CONFIG, "EMAIL_HOST": host}):
            diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.ERROR, diagnostics.status)
        self.assertIn("host_is_placeholder", codes(diagnostics))

    @override_settings(**{**WORKING_CONFIG, "EMAIL_HOST": "localhost"})
    def test_default_local_host_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.WARNING, diagnostics.status)
        self.assertIn("host_is_local", codes(diagnostics))

    @override_settings(**{**WORKING_CONFIG, "EMAIL_USE_SSL": True})
    def test_tls_and_ssl_together_is_an_error(self):
        # Django raises at send time when both are set, so every notification fails.
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.ERROR, diagnostics.status)
        self.assertIn("tls_and_ssl", codes(diagnostics))

    @override_settings(**{**WORKING_CONFIG, "EMAIL_PORT": 465})
    def test_starttls_on_the_implicit_tls_port_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("tls_on_implicit_port", codes(diagnostics))

    @override_settings(
        **{**WORKING_CONFIG, "EMAIL_USE_TLS": False, "EMAIL_USE_SSL": True}
    )
    def test_implicit_tls_on_the_starttls_port_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("ssl_on_starttls_port", codes(diagnostics))

    @override_settings(
        **{**WORKING_CONFIG, "EMAIL_USE_TLS": False, "EMAIL_USE_SSL": False}
    )
    def test_submission_port_without_encryption_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("encryption_missing", codes(diagnostics))

    @override_settings(**{**WORKING_CONFIG, "EMAIL_HOST_PASSWORD": ""})
    def test_user_without_password_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("password_missing", codes(diagnostics))

    @override_settings(**{**WORKING_CONFIG, "EMAIL_HOST_USER": ""})
    def test_password_without_user_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("user_missing", codes(diagnostics))

    @override_settings(**{**WORKING_CONFIG, "EMAIL_TIMEOUT": None})
    def test_missing_timeout_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("timeout_unset", codes(diagnostics))

    @override_settings(
        **{
            **WORKING_CONFIG,
            "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        }
    )
    def test_backend_that_never_delivers_is_an_error(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.ERROR, diagnostics.status)
        self.assertIn("backend_does_not_deliver", codes(diagnostics))

    @override_settings(
        **{**WORKING_CONFIG, "DEFAULT_FROM_EMAIL": "noreply@example.com"}
    )
    def test_shipped_sender_address_is_a_warning(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("from_email_is_placeholder", codes(diagnostics))

    @override_settings(**{**WORKING_CONFIG, "DEFAULT_FROM_EMAIL": "not-an-address"})
    def test_invalid_sender_address_is_an_error(self):
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.ERROR, diagnostics.status)
        self.assertIn("from_email_invalid", codes(diagnostics))

    @override_settings(**WORKING_CONFIG)
    def test_relay_is_fine_but_every_notification_is_disabled(self):
        # The failure the issue reporter hit: nothing is sent and nothing is logged.
        Notification.objects.update(enabled=False)
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(email_diagnostics.ERROR, diagnostics.status)
        self.assertIn("notifications_all_disabled", codes(diagnostics))

    @override_settings(**WORKING_CONFIG)
    def test_empty_notification_catalogue_is_a_warning(self):
        Notification.objects.all().delete()
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertIn("notifications_not_loaded", codes(diagnostics))

    @override_settings(**WORKING_CONFIG)
    def test_recent_email_activity_is_reported(self):
        EmailLog.objects.create(subject="Hi", body="Hi", emails=["user@example.com"])
        diagnostics = email_diagnostics.collect_diagnostics()
        self.assertEqual(1, diagnostics.emails_sent_last_week)
        self.assertIsNotNone(diagnostics.last_email_sent_at)


class SmtpProbeTest(test.APITestCase):
    @override_settings(
        **{
            **WORKING_CONFIG,
            "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        }
    )
    def test_probe_reports_that_a_non_smtp_backend_has_nothing_to_connect_to(self):
        result = email_diagnostics.probe_smtp()
        self.assertFalse(result["success"])
        self.assertIn("does not connect to a relay", result["error"])

    @override_settings(**WORKING_CONFIG)
    def test_probe_reports_the_connection_failure(self):
        with mock.patch("waldur_core.core.email_diagnostics.get_connection") as factory:
            factory.return_value.open.side_effect = ConnectionRefusedError("refused")
            result = email_diagnostics.probe_smtp()
        self.assertFalse(result["success"])
        self.assertIn("ConnectionRefusedError", result["error"])

    @override_settings(**WORKING_CONFIG)
    def test_probe_passes_an_explicit_timeout_so_the_request_cannot_hang(self):
        with mock.patch("waldur_core.core.email_diagnostics.get_connection") as factory:
            result = email_diagnostics.probe_smtp(timeout=3)
        factory.assert_called_once_with(timeout=3, fail_silently=False)
        self.assertTrue(result["success"])
        factory.return_value.close.assert_called_once()

    @override_settings(**WORKING_CONFIG)
    def test_probe_closes_the_socket_left_behind_by_a_failed_handshake(self):
        # Django assigns self.connection before STARTTLS and before AUTH, so a
        # failure there leaves a live socket the probe must not abandon.
        with mock.patch("waldur_core.core.email_diagnostics.get_connection") as factory:
            factory.return_value.open.side_effect = SMTPAuthenticationError(
                535, b"authentication failed"
            )
            result = email_diagnostics.probe_smtp()
        self.assertFalse(result["success"])
        factory.return_value.close.assert_called_once()


class EmailDiagnosticsPermissionTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/debug/email/config/"

    def test_staff_can_read_the_diagnostics(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)

    def test_support_user_cannot_read_the_diagnostics(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_support=True))
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_regular_user_cannot_read_the_diagnostics(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_anonymous_user_cannot_read_the_diagnostics(self):
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_401_UNAUTHORIZED, response.status_code)

    def test_regular_user_cannot_send_a_test_email(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.post("/api/debug/email/send_test/", {})
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


class EmailDiagnosticsApiTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, email="staff@example.com"
        )
        self.client.force_authenticate(self.staff)
        Notification.objects.create(key="users.invitation_created", enabled=True)

    @override_settings(**WORKING_CONFIG)
    def test_config_endpoint_returns_the_audit_without_the_password(self):
        response = self.client.get("/api/debug/email/config/")
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(email_diagnostics.OK, response.data["status"])
        self.assertTrue(response.data["config"]["has_password"])
        self.assertNotIn("password", response.data["config"])
        self.assertEqual(1, response.data["enabled_notification_count"])

    @override_settings(**WORKING_CONFIG)
    def test_probe_endpoint_reports_a_reachable_relay(self):
        with mock.patch("waldur_core.core.email_diagnostics.get_connection"):
            response = self.client.post("/api/debug/email/probe/")
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertTrue(response.data["success"])

    @override_settings(**WORKING_CONFIG)
    def test_test_email_defaults_to_the_requesting_user(self):
        with mock.patch("waldur_core.core.utils.send_mail") as send_mail:
            response = self.client.post("/api/debug/email/send_test/", {})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertTrue(response.data["success"])
        self.assertEqual("staff@example.com", response.data["email"])
        self.assertEqual(["staff@example.com"], send_mail.call_args.kwargs["to"])

    @override_settings(**{**WORKING_CONFIG, "EMAIL_TIMEOUT": None})
    def test_test_email_carries_its_own_timeout(self):
        # Inherited from EMAIL_TIMEOUT this would be None, and a relay that
        # accepts the connection and then stalls would hang the API worker.
        with (
            mock.patch("waldur_core.core.utils.send_mail") as send_mail,
            mock.patch("waldur_core.core.email_diagnostics.get_connection") as factory,
        ):
            self.client.post("/api/debug/email/send_test/", {})
        factory.assert_called_once_with(
            timeout=email_diagnostics.DEFAULT_PROBE_TIMEOUT, fail_silently=False
        )
        self.assertEqual(factory.return_value, send_mail.call_args.kwargs["connection"])

    @override_settings(**WORKING_CONFIG)
    def test_test_email_accepts_an_explicit_recipient(self):
        with mock.patch("waldur_core.core.utils.send_mail") as send_mail:
            response = self.client.post(
                "/api/debug/email/send_test/", {"email": "ops@example.com"}
            )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(["ops@example.com"], send_mail.call_args.kwargs["to"])

    def test_test_email_rejects_a_malformed_recipient(self):
        response = self.client.post(
            "/api/debug/email/send_test/", {"email": "not-an-address"}
        )
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    @override_settings(**WORKING_CONFIG)
    def test_failure_to_send_is_reported_rather_than_raised(self):
        with mock.patch("waldur_core.core.utils.send_mail") as send_mail:
            send_mail.side_effect = ConnectionRefusedError("refused")
            response = self.client.post("/api/debug/email/send_test/", {})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertFalse(response.data["success"])
        self.assertIn("ConnectionRefusedError", response.data["error"])
