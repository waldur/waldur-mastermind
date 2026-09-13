import asyncio
import os
from types import SimpleNamespace
from unittest import mock

from constance.test import override_config
from django.conf import settings
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.authtoken.models import Token

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.web_shell import server, tickets


class DescribeEnvironmentTest(TestCase):
    @override_config(SITE_NAME="Lab portal", HOMEPORT_URL="https://lab.example.org/")
    def test_names_the_site_portal_and_database(self):
        environment = server.describe_environment()

        self.assertEqual(environment["site_name"], "Lab portal")
        self.assertEqual(environment["portal"], "lab.example.org")
        self.assertTrue(environment["host"])
        database = settings.DATABASES["default"]
        self.assertTrue(environment["database"].startswith(f"{database['NAME']}@"))


class CheckAccessTest(TestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.token, _ = Token.objects.get_or_create(user=self.staff)
        self.digest = tickets.token_digest(self.token.key)

    def test_active_staff_with_its_token_may_continue(self):
        self.assertIsNone(server.check_access(self.staff.pk, self.digest))

    def test_logging_out_ends_the_session(self):
        self.token.delete()
        self.assertEqual(
            server.check_access(self.staff.pk, self.digest), "Signed out of Waldur"
        )

    def test_signing_in_again_ends_the_session(self):
        self.token.delete()
        Token.objects.create(user=self.staff)
        self.assertEqual(
            server.check_access(self.staff.pk, self.digest), "Signed out of Waldur"
        )

    def test_losing_staff_ends_the_session(self):
        self.staff.is_staff = False
        self.staff.save(update_fields=["is_staff"])
        self.assertEqual(
            server.check_access(self.staff.pk, self.digest), "No longer staff"
        )

    def test_deactivation_ends_the_session(self):
        self.staff.is_active = False
        self.staff.save(update_fields=["is_active"])
        self.assertEqual(
            server.check_access(self.staff.pk, self.digest), "Account deactivated"
        )

    def test_unbound_session_only_checks_the_account(self):
        self.token.delete()
        self.assertIsNone(server.check_access(self.staff.pk, None))


class OriginTest(SimpleTestCase):
    def test_normalizes_scheme_host_and_default_port(self):
        self.assertEqual(
            server.normalized_origin("https://Waldur.Example.org:443/webshell/"),
            "https://waldur.example.org",
        )
        self.assertEqual(
            server.normalized_origin("http://localhost:18090/webshell/"),
            "http://localhost:18090",
        )

    def test_rejects_other_schemes_and_empty_values(self):
        self.assertIsNone(server.normalized_origin("javascript:alert(1)"))
        self.assertIsNone(server.normalized_origin("null"))
        self.assertIsNone(server.normalized_origin(None))

    def test_accepts_only_the_public_origin(self):
        app = {server.PUBLIC_ORIGIN: "https://waldur.example.org"}

        def request(origin):
            return SimpleNamespace(headers={"Origin": origin}, app=app)

        self.assertTrue(server.origin_allowed(request("https://waldur.example.org")))
        self.assertFalse(server.origin_allowed(request("https://evil.example")))
        self.assertFalse(server.origin_allowed(request("http://waldur.example.org")))

    @override_settings(
        WALDUR_CORE={
            **settings.WALDUR_CORE,
            "WEB_SHELL_URL": "https://waldur.example.org/webshell/",
        }
    )
    def test_allowed_hosts_include_extra_internal_names(self):
        with mock.patch.dict(
            os.environ,
            {"WALDUR_WEB_SHELL_ALLOWED_HOSTS": "api.internal, Other.Internal"},
        ):
            hosts = server.allowed_hosts()

        self.assertTrue(
            {"waldur.example.org", "api.internal", "other.internal", "localhost"}
            <= hosts
        )


def make_session(ws):
    user = SimpleNamespace(
        pk=1, username="staff", full_name="Staff User", email="staff@example.org"
    )
    return server.PtySession(ws, user, "127.0.0.1", 80, 24, token_digest="digest")


class WatchAccessTest(SimpleTestCase):
    def test_closes_the_session_after_repeated_check_failures(self):
        ws = mock.AsyncMock()
        session = make_session(ws)
        with (
            mock.patch.object(server, "ACCESS_CHECK_INTERVAL", 0),
            mock.patch.object(
                server, "check_access", side_effect=OperationalError("gone")
            ),
        ):
            asyncio.run(session._watch_access())

        ws.close.assert_awaited_once_with(
            code=server.CLOSE_REVOKED, message=b"Access could not be verified"
        )

    def test_keeps_checking_after_a_transient_failure(self):
        ws = mock.AsyncMock()
        session = make_session(ws)
        results = [OperationalError("gone"), None, "Signed out of Waldur"]
        with (
            mock.patch.object(server, "ACCESS_CHECK_INTERVAL", 0),
            mock.patch.object(server, "check_access", side_effect=results),
        ):
            asyncio.run(session._watch_access())

        ws.close.assert_awaited_once_with(
            code=server.CLOSE_REVOKED, message=b"Signed out of Waldur"
        )


class SessionStartTest(SimpleTestCase):
    def test_cleans_up_when_the_session_fails_to_start(self):
        ws = mock.AsyncMock()
        ws.send_json.side_effect = ConnectionResetError()
        session = make_session(ws)
        with (
            mock.patch.object(session, "spawn"),
            mock.patch.object(server, "describe_environment", return_value={}),
            mock.patch.object(session, "_close", new=mock.AsyncMock()) as close,
        ):
            with self.assertRaises(ConnectionResetError):
                asyncio.run(session.run())

        close.assert_awaited_once()
