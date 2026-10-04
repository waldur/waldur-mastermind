import json
from unittest import mock

import httpx
from constance.test import override_config
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models
from waldur_mastermind.matrix_chat.matrix_client import MatrixClientError
from waldur_mastermind.matrix_chat.tests import fixtures


# The credentials endpoint is gated on matrix_client.is_enabled(); set the
# Constance values once at the base-class level so every subclass passes
# the gate. Individual tests can still override these inside their methods.
@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class MatrixCredentialsBaseTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.url = "/api/matrix/credentials/"


class MatrixCredentialsAuthTest(MatrixCredentialsBaseTest):
    def test_anonymous_cannot_get_credentials(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @mock.patch(
        "waldur_mastermind.matrix_chat.matrix_client.ensure_user_exists",
        side_effect=MatrixClientError("Failed to register user x: all strategies"),
    )
    def test_failed_provisioning_is_unavailable(self, mock_ensure):
        # Registration fails on the homeserver's side, e.g. behind a proxy's
        # error page, so it is a fault to retry, not the caller's to fix.
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("strategies", str(response.data))

    @mock.patch(
        "waldur_mastermind.matrix_chat.matrix_client.ensure_user_exists",
    )
    def test_user_with_unprovisioned_profile_gets_error(self, mock_ensure):
        user = structure_factories.UserFactory()
        models.MatrixUserProfile.objects.create(
            user=user,
            matrix_user_id=f"@{user.username}:matrix.example.com",
            provisioned=False,
        )
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not been provisioned", response.data["detail"])

    def test_unreachable_homeserver_is_unavailable(self):
        # Provisioning on demand talks to the homeserver, as the session
        # endpoint does, and answers the same way when it can't.
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        for error in [
            httpx.ConnectError("Connection refused"),
            json.JSONDecodeError("Expecting value", "<html>", 0),
        ]:
            with self.subTest(error=type(error).__name__):
                with mock.patch(
                    "waldur_mastermind.matrix_chat.matrix_client.ensure_user_exists",
                    side_effect=error,
                ):
                    response = self.client.get(self.url)

                self.assertEqual(
                    response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE
                )
                self.assertNotIn("Connection refused", str(response.data))


# Secrets the endpoint used to hand to the browser; none may come back.
TOKEN_FIELDS = ("access_token", "refresh_token", "login_token")


@mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
class MatrixCredentialsPasswordTest(MatrixCredentialsBaseTest):
    def test_password_method_returns_credentials(self, mock_config):
        mock_config.MATRIX_EXTERNAL_LOGIN_METHOD = "password"
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        # Empty public URL exercises the fallback path used by deployments
        # where the same URL works server-side and browser-side.
        mock_config.MATRIX_HOMESERVER_PUBLIC_URL = ""
        mock_config.MATRIX_USER_REGISTRATION_SECRET = "test-secret"

        profile = self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["method"], "password")
        self.assertEqual(response.data["homeserver_url"], "https://matrix.example.com")
        self.assertEqual(response.data["matrix_user_id"], profile.matrix_user_id)
        self.assertIn("password", response.data)
        for field in TOKEN_FIELDS + ("oidc_provider_url",):
            self.assertNotIn(field, response.data)

    def test_public_url_overrides_internal_in_credentials(self, mock_config):
        # When the public URL is set distinctly, the credentials response
        # returns that — backend bot code continues to call the internal
        # URL but the browser is told the Caddy-proxied address.
        mock_config.MATRIX_EXTERNAL_LOGIN_METHOD = "password"
        mock_config.MATRIX_HOMESERVER_URL = "http://tuwunel.internal:6167"
        mock_config.MATRIX_HOMESERVER_PUBLIC_URL = "https://waldur.example.com"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = "test-secret"

        self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["homeserver_url"], "https://waldur.example.com")

    def test_password_method_without_secret_returns_error(self, mock_config):
        mock_config.MATRIX_EXTERNAL_LOGIN_METHOD = "password"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = ""

        self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("MATRIX_USER_REGISTRATION_SECRET", response.data["detail"])


@mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
class MatrixCredentialsWithoutPasswordTest(MatrixCredentialsBaseTest):
    def _get(self, mock_config, method):
        mock_config.MATRIX_EXTERNAL_LOGIN_METHOD = method
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_PUBLIC_URL = ""
        mock_config.MATRIX_USER_REGISTRATION_SECRET = "test-secret"
        self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        return self.client.get(self.url)

    def test_oidc_method_returns_identity_only(self, mock_config):
        response = self._get(mock_config, "oidc")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "method": "oidc",
                "homeserver_url": "https://matrix.example.com",
                "matrix_user_id": self.fixture.matrix_user_profile.matrix_user_id,
            },
        )

    def test_none_method_returns_no_secret(self, mock_config):
        response = self._get(mock_config, "none")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["method"], "none")
        self.assertNotIn("password", response.data)
        for field in TOKEN_FIELDS:
            self.assertNotIn(field, response.data)

    def test_room_uuid_no_longer_hands_out_room_or_token(self, mock_config):
        models.MatrixRoomMember.objects.create(
            room=self.fixture.matrix_room,
            user=self.fixture.admin,
            matrix_user_id="@admin:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )
        mock_config.MATRIX_EXTERNAL_LOGIN_METHOD = "none"
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_PUBLIC_URL = ""
        self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.get(
            self.url, {"room_uuid": self.fixture.matrix_room.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("room_id", response.data)
        self.assertNotIn("access_token", response.data)

    def test_unknown_method_returns_error(self, mock_config):
        response = self._get(mock_config, "token")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unknown login method", response.data["detail"])
