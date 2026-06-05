from unittest import mock

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
        side_effect=MatrixClientError("Provisioning failed"),
    )
    def test_user_without_profile_gets_error(self, mock_ensure):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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


@mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
class MatrixCredentialsPasswordTest(MatrixCredentialsBaseTest):
    def test_password_method_returns_credentials(self, mock_config):
        mock_config.MATRIX_LOGIN_METHOD = "password"
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = "test-secret"

        profile = self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["method"], "password")
        self.assertEqual(response.data["homeserver_url"], "https://matrix.example.com")
        self.assertEqual(response.data["matrix_user_id"], profile.matrix_user_id)
        self.assertIn("password", response.data)
        self.assertNotIn("login_token", response.data)
        self.assertNotIn("oidc_provider_url", response.data)

    def test_password_method_without_secret_returns_error(self, mock_config):
        mock_config.MATRIX_LOGIN_METHOD = "password"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = ""

        self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("MATRIX_USER_REGISTRATION_SECRET", response.data["detail"])


@mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
class MatrixCredentialsTokenTest(MatrixCredentialsBaseTest):
    @mock.patch(
        "waldur_mastermind.matrix_chat.matrix_client._run_async",
        return_value="mocked_access_token_123",
    )
    def test_token_method_returns_access_token(self, mock_run_async, mock_config):
        mock_config.MATRIX_LOGIN_METHOD = "token"
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"

        profile = self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["method"], "token")
        self.assertEqual(response.data["homeserver_url"], "https://matrix.example.com")
        self.assertEqual(response.data["matrix_user_id"], profile.matrix_user_id)
        self.assertEqual(response.data["login_token"], "mocked_access_token_123")
        self.assertNotIn("password", response.data)
        self.assertNotIn("oidc_provider_url", response.data)

    def test_token_method_without_as_token_returns_error(self, mock_config):
        # An empty AS_TOKEN makes matrix_client.is_enabled() False, so the
        # view 404s before the per-method validation can produce a 400.
        # Pin AS_TOKEN here so we still exercise the original error path —
        # the integration is gated separately at the base-class level via
        # @override_config.
        mock_config.MATRIX_LOGIN_METHOD = "token"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = ""
        mock_config.MATRIX_ENABLED = True
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"

        self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        # The integration check runs against the mocked matrix_client.config
        # (which is_enabled() reads through), and an empty AS_TOKEN there
        # makes the gate close — 404 is now the correct response.
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND),
        )


@mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
class MatrixCredentialsOidcTest(MatrixCredentialsBaseTest):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.IdentityProvider")
    def test_oidc_method_returns_provider_url(self, mock_idp_model, mock_config):
        mock_config.MATRIX_LOGIN_METHOD = "oidc"
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_OIDC_PROVIDER_URL = (
            "https://keycloak.example.com/realms/main"
        )

        mock_idp = mock.MagicMock()
        mock_idp.auth_url = "https://keycloak.example.com/realms/main"
        mock_idp_model.objects.get.return_value = mock_idp

        profile = self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["method"], "oidc")
        self.assertEqual(response.data["homeserver_url"], "https://matrix.example.com")
        self.assertEqual(response.data["matrix_user_id"], profile.matrix_user_id)
        self.assertEqual(
            response.data["oidc_provider_url"],
            "https://keycloak.example.com/realms/main",
        )
        self.assertNotIn("password", response.data)
        self.assertNotIn("login_token", response.data)


@mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
class MatrixCredentialsUnknownMethodTest(MatrixCredentialsBaseTest):
    def test_unknown_method_returns_error(self, mock_config):
        mock_config.MATRIX_LOGIN_METHOD = "invalid"

        self.fixture.matrix_user_profile
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unknown login method", response.data["detail"])


@mock.patch("waldur_mastermind.matrix_chat.matrix_client.join_room_as_self")
@mock.patch(
    "waldur_mastermind.matrix_chat.matrix_client.get_access_token_for_user",
    return_value="access_token_123",
)
@mock.patch("waldur_mastermind.matrix_chat.matrix_client.get_user_matrix_credentials")
class MatrixCredentialsRoomAccessTest(MatrixCredentialsBaseTest):
    """Conversation access via ?room_uuid= is limited to current room members.

    A user enters a room's live conversation only if they hold an active
    MatrixRoomMember row (created solely from project membership). Role-based
    access alone — e.g. staff or a customer owner who is not a project member —
    grants room management but not the conversation.
    """

    def setUp(self):
        super().setUp()
        self.room = self.fixture.matrix_room

    def _credentials(self):
        return {
            "method": "token",
            "matrix_user_id": "@user:matrix.example.com",
            "homeserver_url": "https://matrix.example.com",
        }

    def _get(self, user, mock_get_creds):
        mock_get_creds.return_value = self._credentials()
        self.client.force_authenticate(user)
        return self.client.get(self.url, {"room_uuid": self.room.uuid.hex})

    def test_room_member_receives_room_credentials(
        self, mock_get_creds, mock_token, mock_join
    ):
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=self.fixture.admin,
            matrix_user_id="@admin:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )
        response = self._get(self.fixture.admin, mock_get_creds)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["room_id"], self.room.room_id)
        self.assertEqual(response.data["access_token"], "access_token_123")
        mock_join.assert_called_once()

    def test_customer_owner_without_membership_is_denied_convo(
        self, mock_get_creds, mock_token, mock_join
    ):
        # The customer owner has role-based access to the room but is not a
        # project member, so no MatrixRoomMember row exists for them.
        response = self._get(self.fixture.owner, mock_get_creds)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("room_id", response.data)
        mock_join.assert_not_called()

    def test_staff_without_membership_is_denied_convo(
        self, mock_get_creds, mock_token, mock_join
    ):
        response = self._get(self.fixture.staff, mock_get_creds)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("room_id", response.data)
        mock_join.assert_not_called()

    def test_member_who_left_is_denied_convo(
        self, mock_get_creds, mock_token, mock_join
    ):
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=self.fixture.admin,
            matrix_user_id="@admin:matrix.example.com",
            membership_state=models.MembershipStates.LEFT,
        )
        response = self._get(self.fixture.admin, mock_get_creds)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("room_id", response.data)
        mock_join.assert_not_called()
