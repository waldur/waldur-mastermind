from datetime import timedelta
from unittest import mock

from django.core import serializers
from django.utils import timezone
from freezegun import freeze_time
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.common import Request
from rest_framework import test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories
from waldur_mastermind.mcp import tasks
from waldur_mastermind.mcp.models import ConnectedApp
from waldur_mastermind.mcp.oauth import MCPOAuth2Validator
from waldur_mastermind.mcp.tests.test_oauth import (
    CIMD_CLIENT_ID,
    MCP_URL,
    AccessToken,
    Application,
    Grant,
    OAuthTestMixin,
    RefreshToken,
)
from waldur_mastermind.mcp.tests.test_server import create_pat
from waldur_mastermind.mcp.views import ConnectedAppViewSet

URL = "/api/oauth-connected-apps/"


def detail_url(connected_app):
    return f"{URL}{connected_app.uuid.hex}/"


def is_accepted(access_token):
    """Whether DOT would accept the bearer token."""
    request = Request("https://api.example.com/mcp")
    return OAuth2Validator().validate_bearer_token(access_token, ["mcp:read"], request)


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class ConnectedAppTestCase(OAuthTestMixin, test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory()
        self.application = self.create_public_client(
            registration_source=Application.RegistrationSource.DCR
        )
        # Not a Waldur API token, which expires before the clock moves in some tests.
        self.client.force_authenticate(self.user)

    def connect(self, application=None, user=None, scope="mcp:read"):
        """Authorise the client and exchange the code, as a real client would."""
        application = application or self.application
        grant = self.create_grant(application, user or self.user, scope=scope)
        response = self.exchange_code(application, grant)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def connected_app(self, application=None, user=None):
        return ConnectedApp.objects.get(
            application=application or self.application, user=user or self.user
        )

    def list_apps(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()


class ListTest(ConnectedAppTestCase):
    def test_lists_an_authorised_client(self):
        self.connect()
        connected_app = self.connected_app()

        [app] = self.list_apps()

        self.assertEqual(app["uuid"], connected_app.uuid.hex)
        self.assertEqual(app["client_name"], "Claude")
        self.assertIsNone(app["client_host"])
        self.assertEqual(app["redirect_hosts"], ["claude.ai"])
        self.assertEqual(app["registration_source"], "dcr")
        self.assertEqual(
            app["scopes"],
            [{"name": "mcp:read", "description": "Read your Waldur data"}],
        )
        self.assertEqual(app["authorized_at"], connected_app.created.isoformat())
        self.assertIsNone(app["last_used_at"])

    def test_cimd_client_shows_its_host(self):
        application = self.create_public_client(
            client_id=CIMD_CLIENT_ID,
            registration_source=Application.RegistrationSource.CIMD,
        )
        self.connect(application)

        [app] = self.list_apps()

        self.assertEqual(app["client_host"], "client.example.com")
        self.assertEqual(app["registration_source"], "cimd")

    def test_lists_every_redirect_host_once(self):
        # A copycat can register the genuine client's URI first.
        self.application.redirect_uris = (
            "https://claude.ai/api/mcp/auth_callback "
            "https://evil.example/callback "
            "https://claude.ai/other "
            "com.example.app:/callback"
        )
        self.application.save()
        self.create_grant(self.application, self.user)

        [app] = self.list_apps()

        self.assertEqual(app["redirect_hosts"], ["claude.ai", "evil.example"])

    def test_hosts_are_shown_as_the_browser_looks_them_up(self):
        # claude.ai spelled with a Cyrillic "c" is another domain.
        Application.objects.filter(pk=self.application.pk).update(
            redirect_uris="https://\u0441laude.ai/callback "
            "https://claude\u200b.ai/callback "
            "https://fa\u00df.example/callback "
            "https://exa\u202emple.com/callback"
        )
        cimd_application = self.create_public_client(
            client_id="https://\u0441laude.ai/metadata.json",
            registration_source=Application.RegistrationSource.CIMD,
        )
        self.create_grant(self.application, self.user)
        self.create_grant(cimd_application, self.user)

        cimd_app, dcr_app = self.list_apps()

        self.assertEqual(cimd_app["client_host"], "xn--laude-0ye.ai")
        self.assertEqual(
            dcr_app["redirect_hosts"],
            [
                "xn--laude-0ye.ai",
                # Browsers drop a zero-width space, too.
                "claude.ai",
                # Escaped where Python's codec and browsers disagree, or
                # where no browser could look it up.
                "fa\\xdf.example",
                "exa\\u202emple.com",
            ],
        )

    def test_a_uri_that_no_longer_parses_is_skipped(self):
        # Registration refuses it, but a stricter Python may refuse a stored one.
        Application.objects.filter(pk=self.application.pk).update(
            name="", redirect_uris="https://[foo/callback https://claude.ai/callback"
        )
        cimd_application = self.create_public_client(
            client_id="https://[foo/metadata.json",
            registration_source=Application.RegistrationSource.CIMD,
        )
        self.create_grant(self.application, self.user)
        self.create_grant(cimd_application, self.user)

        apps = self.list_apps()

        self.assertEqual(
            [(app["client_host"], app["redirect_hosts"]) for app in apps],
            [(None, ["claude.ai"]), (None, ["claude.ai"])],
        )
        self.assertEqual(apps[1]["client_name"], "claude.ai")

    def test_nameless_client_is_shown_by_its_redirect_host(self):
        self.application.name = ""
        self.application.save()
        self.connect()

        [app] = self.list_apps()

        self.assertEqual(app["client_name"], "claude.ai")

    def test_client_name_drops_control_and_bidi_characters(self):
        self.application.name = "Claude\u202e\nDesktop\x07"
        self.application.save()
        self.connect()

        [app] = self.list_apps()

        self.assertEqual(app["client_name"], "Claude Desktop")

    def test_lists_only_the_callers_own_apps(self):
        other_user = factories.UserFactory()
        other_application = self.create_public_client()
        self.connect(other_application, user=other_user)
        # The same client, authorised by both users.
        self.connect(user=other_user)
        self.connect()

        [app] = self.list_apps()

        self.assertEqual(app["uuid"], self.connected_app().uuid.hex)

    def test_a_pending_code_is_listed(self):
        self.create_grant(self.application, self.user)

        [app] = self.list_apps()

        self.assertEqual(app["uuid"], self.connected_app().uuid.hex)

    def test_an_expired_code_is_not_listed(self):
        grant = self.create_grant(self.application, self.user)
        Grant.objects.filter(pk=grant.pk).update(
            expires=timezone.now() - timedelta(seconds=1)
        )

        self.assertEqual(self.list_apps(), [])

    def test_an_expired_access_token_with_a_working_refresh_token_is_listed(self):
        self.connect()

        with freeze_time(timezone.now() + timedelta(days=29)):
            self.assertEqual(len(self.list_apps()), 1)

    def test_a_refresh_token_past_its_lifetime_is_not_listed(self):
        tokens = self.connect()

        with freeze_time(timezone.now() + timedelta(days=31)):
            self.assertEqual(self.list_apps(), [])
            # DOT agrees that the refresh token no longer works.
            response = self.refresh(self.application, tokens["refresh_token"])
            self.assertEqual(response.status_code, 400)

    def test_a_revoked_refresh_token_is_not_listed(self):
        self.connect()
        RefreshToken.objects.get().revoke()

        self.assertEqual(self.list_apps(), [])

    def test_scopes_of_all_working_tokens_are_listed(self):
        self.connect()
        AccessToken.objects.create(
            user=self.user,
            application=self.application,
            token="extra",
            scope="mcp:read mcp:staff",
            expires=timezone.now() + timedelta(hours=1),
        )

        [app] = self.list_apps()

        self.assertEqual(
            app["scopes"],
            [
                {"name": "mcp:read", "description": "Read your Waldur data"},
                {"name": "mcp:staff", "description": "mcp:staff"},
            ],
        )

    def test_refresh_keeps_the_connection(self):
        tokens = self.connect()
        authorized_at = self.connected_app().created

        response = self.refresh(self.application, tokens["refresh_token"])

        self.assertEqual(response.status_code, 200, response.content)
        [app] = self.list_apps()
        self.assertEqual(app["authorized_at"], authorized_at.isoformat())

    def test_listed_while_mcp_is_off(self):
        # So the user can still revoke apps an admin may turn MCP back on for.
        self.connect()

        with override_config(MCP_ENABLED=False):
            self.assertEqual(len(self.list_apps()), 1)


class RevokeTest(ConnectedAppTestCase):
    def test_revoked_tokens_no_longer_work(self):
        tokens = self.connect()
        self.assertTrue(is_accepted(tokens["access_token"]))

        response = self.client.delete(detail_url(self.connected_app()))

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(is_accepted(tokens["access_token"]))
        response = self.refresh(self.application, tokens["refresh_token"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertEqual(self.list_apps(), [])

    def test_revoke_deletes_codes_tokens_and_the_connection(self):
        tokens = self.connect()
        self.refresh(self.application, tokens["refresh_token"])
        self.create_grant(self.application, self.user)

        self.client.delete(detail_url(self.connected_app()))

        user_tokens = {"user": self.user, "application": self.application}
        self.assertFalse(AccessToken.objects.filter(**user_tokens).exists())
        self.assertFalse(RefreshToken.objects.filter(**user_tokens).exists())
        self.assertFalse(Grant.objects.filter(**user_tokens).exists())
        self.assertFalse(ConnectedApp.objects.exists())

    def test_a_refresh_racing_a_revoke_gets_invalid_grant(self):
        tokens = self.connect()
        validate = MCPOAuth2Validator.validate_refresh_token

        def validate_then_revoke(validator, *args, **kwargs):
            valid = validate(validator, *args, **kwargs)
            # The revoke lands between the refresh's check and its lock.
            self.connected_app().revoke()
            return valid

        with mock.patch.object(
            MCPOAuth2Validator, "validate_refresh_token", validate_then_revoke
        ):
            response = self.refresh(self.application, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertFalse(AccessToken.objects.exists())

    def test_without_rotation_a_refresh_racing_a_revoke_gets_invalid_grant(self):
        # DOT then reuses the access token, which the revoke deleted.
        tokens = self.connect()
        validate = MCPOAuth2Validator.validate_refresh_token

        def validate_then_revoke(validator, *args, **kwargs):
            valid = validate(validator, *args, **kwargs)
            self.connected_app().revoke()
            return valid

        with (
            mock.patch.object(
                MCPOAuth2Validator, "rotate_refresh_token", return_value=False
            ),
            mock.patch.object(
                MCPOAuth2Validator, "validate_refresh_token", validate_then_revoke
            ),
        ):
            response = self.refresh(self.application, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertFalse(AccessToken.objects.exists())

    def test_a_code_exchange_racing_a_revoke_gets_invalid_grant(self):
        grant = self.create_grant(self.application, self.user)
        confirm = MCPOAuth2Validator.confirm_redirect_uri

        def confirm_then_revoke(validator, *args, **kwargs):
            confirmed = confirm(validator, *args, **kwargs)
            # The revoke lands between the exchange's last check and its save.
            self.connected_app().revoke()
            return confirmed

        with mock.patch.object(
            MCPOAuth2Validator, "confirm_redirect_uri", confirm_then_revoke
        ):
            response = self.exchange_code(self.application, grant)

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        # Not saved, so not listed as a connection nobody can use.
        self.assertFalse(AccessToken.objects.exists())
        self.assertEqual(self.list_apps(), [])

    def test_revoke_keeps_other_clients_and_other_users(self):
        other_user = factories.UserFactory()
        other_application = self.create_public_client()
        other_users_tokens = self.connect(user=other_user)
        other_clients_tokens = self.connect(other_application)
        self.connect()

        self.client.delete(detail_url(self.connected_app()))

        self.assertTrue(is_accepted(other_users_tokens["access_token"]))
        self.assertTrue(is_accepted(other_clients_tokens["access_token"]))
        self.assertEqual(ConnectedApp.objects.count(), 2)

    def test_revoke_is_audited(self):
        self.application.redirect_uris = (
            "https://claude.ai/callback https://example.com/callback"
        )
        self.application.save()
        self.connect()
        AccessToken.objects.create(
            user=self.user,
            application=self.application,
            token="extra",
            scope="mcp:staff mcp:read",
            expires=timezone.now() + timedelta(hours=1),
        )

        self.client.delete(detail_url(self.connected_app()))

        event = Event.objects.get(event_type="oauth_connected_app_revoked")
        self.assertEqual(
            event.message,
            f"User {self.user.username} has revoked the access of OAuth client "
            "Claude to Waldur.",
        )
        self.assertEqual(event.context["client_name"], "Claude")
        self.assertEqual(event.context["client_id"], self.application.client_id)
        self.assertEqual(event.context["redirect_hosts"], ["claude.ai", "example.com"])
        self.assertEqual(event.context["scope"], "mcp:read mcp:staff")

    def test_client_name_with_braces_is_kept_as_written(self):
        self.application.name = "{affected_user_email}"
        self.application.save()
        self.connect()

        self.client.delete(detail_url(self.connected_app()))

        event = Event.objects.get(event_type="oauth_connected_app_revoked")
        self.assertIn("client {affected_user_email} to", event.message)

    def test_a_concurrent_second_revoke_is_not_found(self):
        self.connect()
        connected_app = self.connected_app()
        # Another request revokes it after this one has looked it up.
        ConnectedApp.objects.get().revoke()

        with mock.patch.object(
            ConnectedAppViewSet, "get_object", return_value=connected_app
        ):
            response = self.client.delete(detail_url(connected_app))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            Event.objects.filter(event_type="oauth_connected_app_revoked").exists()
        )

    def test_another_users_app_is_not_found(self):
        other_user = factories.UserFactory()
        self.connect(user=other_user)

        response = self.client.delete(detail_url(self.connected_app(user=other_user)))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(RefreshToken.objects.filter(user=other_user).exists())

    def test_an_ended_connection_is_not_found(self):
        self.connect()
        connected_app = self.connected_app()
        RefreshToken.objects.get().revoke()

        response = self.client.delete(detail_url(connected_app))

        self.assertEqual(response.status_code, 404)


class AuthenticationTest(ConnectedAppTestCase):
    def assert_refused(self, connected_app):
        list_response = self.client.get(URL)
        delete_response = self.client.delete(detail_url(connected_app))

        self.assertEqual(list_response.status_code, 403)
        self.assertIn("own sign-in", list_response.json()["detail"])
        self.assertEqual(delete_response.status_code, 403)
        self.assertTrue(RefreshToken.objects.filter(user=self.user).exists())

    @override_config(PAT_ENABLED=True)
    def test_personal_access_token_is_refused(self):
        self.connect()
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        pat = create_pat(self.user)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat.plaintext}")

        self.assert_refused(self.connected_app())

    def test_impersonated_session_is_refused(self):
        self.connect()
        staff = factories.UserFactory(is_staff=True)
        token, _ = Token.objects.get_or_create(user=staff)
        self.client.force_authenticate(None)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {token.key}",
            HTTP_X_IMPERSONATED_USER_UUID=self.user.uuid.hex,
        )

        self.assert_refused(self.connected_app())

    def test_oidc_access_token_is_refused(self):
        # E.g. one the identity provider issued to another of its clients.
        self.connect()
        self.client.force_authenticate(self.user, token={"sub": "someone"})

        self.assert_refused(self.connected_app())

    def test_anonymous_is_refused(self):
        self.client.force_authenticate(None)

        response = self.client.get(URL)

        self.assertEqual(response.status_code, 401)

    def test_waldur_api_token_is_accepted(self):
        self.connect()
        token, _ = Token.objects.get_or_create(user=self.user)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        response = self.client.get(URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_session_is_accepted(self):
        self.connect()
        self.client.force_authenticate(None)
        self.client.force_login(self.user)

        response = self.client.get(URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)


class ConnectionRecordTest(ConnectedAppTestCase):
    def test_first_code_records_the_connection(self):
        with freeze_time("2026-11-01T10:00:00Z"):
            self.create_grant(self.application, self.user)

        connected_app = self.connected_app()
        self.assertEqual(connected_app.created.isoformat(), "2026-11-01T10:00:00+00:00")
        self.assertIsNone(connected_app.last_used_at)

    def test_later_codes_keep_the_first_authorisation_time(self):
        with freeze_time("2026-11-01T10:00:00Z"):
            self.connect()
        first = self.connected_app()

        Grant.objects.create(
            application=self.application,
            user=self.user,
            code="second",
            expires=timezone.now() + timedelta(minutes=1),
            redirect_uri=self.application.redirect_uris,
        )

        self.assertEqual(ConnectedApp.objects.get(), first)
        self.assertEqual(self.connected_app().created, first.created)

    def test_a_missing_record_comes_back_on_refresh(self):
        tokens = self.connect()
        ConnectedApp.objects.all().delete()

        response = self.refresh(self.application, tokens["refresh_token"])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.list_apps()), 1)

    def test_a_token_without_a_code_records_the_connection(self):
        AccessToken.objects.create(
            user=self.user,
            application=self.application,
            token="no-code",
            scope="mcp:read",
            expires=timezone.now() + timedelta(hours=1),
        )

        self.assertEqual(len(self.list_apps()), 1)

    def test_a_token_without_a_user_records_nothing(self):
        AccessToken.objects.create(
            application=self.application,
            token="no-user",
            scope="mcp:read",
            expires=timezone.now() + timedelta(hours=1),
        )

        self.assertFalse(ConnectedApp.objects.exists())

    def test_authorising_again_after_the_connection_ended_starts_over(self):
        with freeze_time("2026-11-01T10:00:00Z"):
            self.connect()
        # The client signs out through /oauth/revoke_token/.
        RefreshToken.objects.get().revoke()

        with freeze_time("2026-11-01T12:00:00Z"):
            self.create_grant(self.application, self.user)

        self.assertEqual(
            self.connected_app().created.isoformat(), "2026-11-01T12:00:00+00:00"
        )

    def test_cleanup_removes_ended_connections_only(self):
        self.connect()
        ended_application = self.create_public_client()
        self.connect(ended_application)
        RefreshToken.objects.filter(application=ended_application).get().revoke()

        tasks.clear_expired_oauth_tokens()

        self.assertEqual(
            list(ConnectedApp.objects.values_list("application", flat=True)),
            [self.application.pk],
        )

    def test_loading_fixtures_records_nothing(self):
        grant = self.create_grant(self.application, self.user)
        dump = serializers.serialize("json", [grant])
        grant.delete()
        ConnectedApp.objects.all().delete()

        for fixture in serializers.deserialize("json", dump):
            fixture.save()

        self.assertTrue(Grant.objects.exists())
        self.assertFalse(ConnectedApp.objects.exists())

    def test_deleting_the_client_deletes_the_connection(self):
        self.connect()

        self.application.delete()

        self.assertFalse(ConnectedApp.objects.exists())
