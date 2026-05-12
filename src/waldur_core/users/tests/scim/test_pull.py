"""Tests for the on-demand outbound SCIM pull."""

from io import StringIO
from unittest import mock

from constance.test.unittest import override_config
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.scim.pull import service as pull_service
from waldur_core.users.tests.scim.conftest import make_staff_token


def _scim_user(user_name="alice", email="alice@example.com", given="Alice"):
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": "remote-1",
        "userName": user_name,
        "externalId": "okta-42",
        "name": {"givenName": given, "familyName": "Smith"},
        "emails": [{"value": email, "primary": True}],
    }


@override_config(
    SCIM_PULL_API_URL="https://remote.example.com/scim/v2",
    SCIM_PULL_API_KEY="secret",
    SCIM_PULL_SOURCE_NAME="scim:remote",
    SCIM_INBOUND_ALLOWED_ATTRIBUTES=[
        "first_name",
        "last_name",
        "email",
    ],
)
class ScimPullServiceTest(test.APITestCase):
    def test_pull_user_attributes_records_source(self):
        user = structure_factories.UserFactory(username="alice")
        with mock.patch(
            "waldur_core.users.scim.pull.service.ScimPullClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.get_user_by_username.return_value = _scim_user()
            changed = pull_service.pull_user_attributes(user)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Alice")
        self.assertEqual(user.email, "alice@example.com")
        self.assertEqual(user.attribute_sources["first_name"]["source"], "scim:remote")
        self.assertEqual(user.attribute_sources["externalId"]["value"], "okta-42")
        self.assertIn("first_name", changed)
        self.assertIn("externalId", changed)

    def test_pull_user_attributes_no_remote_user(self):
        user = structure_factories.UserFactory(username="alice")
        with mock.patch(
            "waldur_core.users.scim.pull.service.ScimPullClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.get_user_by_username.return_value = None
            changed = pull_service.pull_user_attributes(user)
        self.assertEqual(changed, set())


class ScimPullNotConfiguredTest(test.APITestCase):
    def test_management_command_errors_when_not_configured(self):
        with override_config(SCIM_PULL_API_URL="", SCIM_PULL_API_KEY=""):
            with self.assertRaises(CommandError):
                call_command("scim_pull_user", username="anyone")


@override_config(
    SCIM_PULL_API_URL="https://remote.example.com/scim/v2",
    SCIM_PULL_API_KEY="secret",
    SCIM_PULL_SOURCE_NAME="scim:remote",
    SCIM_INBOUND_ALLOWED_ATTRIBUTES=[
        "first_name",
        "last_name",
        "email",
    ],
)
class ScimPullManagementCommandTest(test.APITestCase):
    def test_pull_single_user(self):
        structure_factories.UserFactory(username="alice")
        out = StringIO()
        with mock.patch(
            "waldur_core.users.scim.pull.service.ScimPullClient"
        ) as mock_client_cls:
            mock_client_cls.return_value.get_user_by_username.return_value = (
                _scim_user()
            )
            call_command("scim_pull_user", username="alice", stdout=out)
        self.assertIn("Pulled alice", out.getvalue())

    def test_pull_unknown_user_errors(self):
        with mock.patch("waldur_core.users.scim.pull.service.ScimPullClient"):
            with self.assertRaises(CommandError):
                call_command("scim_pull_user", username="ghost")


@override_config(
    SCIM_PULL_API_URL="https://remote.example.com/scim/v2",
    SCIM_PULL_API_KEY="secret",
    SCIM_PULL_SOURCE_NAME="scim:remote",
    SCIM_INBOUND_ALLOWED_ATTRIBUTES=[
        "first_name",
        "last_name",
        "email",
    ],
)
class ScimPullViewActionTest(test.APITestCase):
    def test_staff_can_trigger_pull(self):
        token_key, svc = make_staff_token()
        target = structure_factories.UserFactory(username="alice")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token_key}")
        with mock.patch(
            "waldur_core.users.scim.pull.service.ScimPullClient"
        ) as mock_client_cls:
            mock_client_cls.return_value.get_user_by_username.return_value = (
                _scim_user()
            )
            response = self.client.post(
                f"/api/users/{target.uuid.hex}/pull_scim_attributes/"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("first_name", response.data["changed_fields"])

    def test_non_staff_forbidden(self):
        user = structure_factories.UserFactory(is_staff=False)
        token, _ = Token.objects.get_or_create(user=user)
        target = structure_factories.UserFactory(username="alice")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.post(
            f"/api/users/{target.uuid.hex}/pull_scim_attributes/"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
