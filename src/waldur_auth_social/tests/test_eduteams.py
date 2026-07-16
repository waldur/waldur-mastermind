from unittest import mock

import responses
from constance.test import override_config
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.core.models import SshPublicKey, User
from waldur_core.logging.enums import EventType
from waldur_core.structure.tests import factories as structure_factories


@override_settings(
    WALDUR_AUTH_SOCIAL={
        "REMOTE_EDUTEAMS_REFRESH_TOKEN": "28c5353b8bb34984a8bd4169ba94c606",
        "REMOTE_EDUTEAMS_USERINFO_URL": "https://proxy.acc.researcher-access.org/api/userinfo",
        "REMOTE_EDUTEAMS_TOKEN_URL": "https://proxy.acc.researcher-access.org/OIDC/token",
        "REMOTE_EDUTEAMS_CLIENT_ID": "WaldurId",
        "REMOTE_EDUTEAMS_SECRET": "WaldurSecret",
        "REMOTE_EDUTEAMS_ENABLED": True,
    }
)
class RemoteEduteamsTest(test.APITestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("auth_remote_eduteams")
        self.valid_cuid = (
            "87b867ff52768f8c11f1501598c2dd1e526fe7f0@acc.researcher-access.org"
        )
        self.user_url = (
            f"https://proxy.acc.researcher-access.org/api/userinfo/{self.valid_cuid}"
        )
        cache.delete("REMOTE_EDUTEAMS_ACCESS_TOKEN")

    def setup_token_response(self):
        responses.add(
            method="POST",
            url="https://proxy.acc.researcher-access.org/OIDC/token",
            json={"access_token": "random_token", "refresh_token": "new_refresh_token"},
        )

    def setup_user_info(self):
        self.setup_token_response()
        responses.add(
            method="GET",
            url=self.user_url,
            json={
                "voperson_id": "87b867ff52768f8c11f1501598c2dd1e526fe7f0@acc.researcher-access.org",
                "name": "John Snow",
                "given_name": "John",
                "family_name": "Snow",
                "mail": ["john@snow.me"],
                "ssh_public_key": [
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHaD5EERMoDJvjH9p4wR19MFX6y+VI6J6432cI5x4PjT"
                ],
            },
        )

    @responses.activate
    def test_unauthorized_user_can_not_sync_remote_users(self):
        user = structure_factories.UserFactory()
        self.client.force_login(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data,
            "Only staff and identity manager are allowed to sync remote users.",
        )

    @responses.activate
    def test_when_user_does_not_exist_remote_api_is_called(self):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_identity_manager=True)
        self.client.force_login(user)

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        new_user = User.objects.get(username=self.valid_cuid)
        self.assertEqual(new_user.email, "john@snow.me")
        self.assertEqual(new_user.full_name, "John Snow")

        keys = SshPublicKey.objects.filter(user=new_user)
        self.assertEqual(keys.count(), 1)

    @responses.activate
    def test_staff_can_trigger_remote_user_sync(self):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @responses.activate
    def test_when_user_exists_it_is_updated(self):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        remote_user = structure_factories.UserFactory(
            username=self.valid_cuid, email="foo@example.com"
        )

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        remote_user.refresh_from_db()
        self.assertEqual(remote_user.email, "john@snow.me")
        self.assertEqual(remote_user.full_name, "John Snow")

        keys = SshPublicKey.objects.filter(user=remote_user)
        self.assertEqual(keys.count(), 1)

    @responses.activate
    @mock.patch("waldur_core.logging.event_logger.emit")
    def test_when_user_is_updated_events_are_emitted(
        self, mock_event_logger: mock.Mock
    ):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        structure_factories.UserFactory(
            first_name="Steve",
            last_name="Jobs",
            username=self.valid_cuid,
            email="steve@jobs.com",
        )
        mock_event_logger.reset_mock()

        self.client.post(self.url, {"cuid": self.valid_cuid})

        mock_event_logger.assert_any_call(
            (
                "User {affected_user_username} has been updated."
                " Source: isd:eduteams. Details:\n"
                "email: steve@jobs.com -> john@snow.me\n"
                "first_name: Steve -> John\n"
                "last_name: Jobs -> Snow\n"
                "active_isds: [] -> ['isd:eduteams']"
            ),
            event_type=EventType.USER_UPDATE_SUCCEEDED,
            event_context={"affected_user": mock.ANY},
            scopes=mock.ANY,
        )

    @responses.activate
    def test_when_user_is_not_found_it_is_disabled(self):
        self.setup_token_response()
        valid_cuid = (
            "17b867ff52768f8c11f1501598c2dd1e526fe7f0@acc.researcher-access.org"
        )
        user_url = f"https://proxy.acc.researcher-access.org/api/userinfo/{valid_cuid}"
        responses.add(
            method="GET",
            url=user_url,
            status=404,
        )
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        remote_user = structure_factories.UserFactory(
            username=valid_cuid, email="foo@example.com"
        )

        response = self.client.post(self.url, {"cuid": valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        remote_user.refresh_from_db()
        self.assertFalse(remote_user.is_active)

    @responses.activate
    def test_notifications_disabled_for_newly_created_user(self):
        """Test that notifications are disabled for newly created remote eduTEAMS users."""
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        new_user = User.objects.get(username=self.valid_cuid)
        self.assertFalse(new_user.notifications_enabled)

    @responses.activate
    def test_notifications_preserved_for_existing_user(self):
        """Test that notifications are preserved for existing remote eduTEAMS users."""
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        existing_user = structure_factories.UserFactory(
            username=self.valid_cuid,
            email="foo@example.com",
            notifications_enabled=True,
        )

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        existing_user.refresh_from_db()
        self.assertTrue(existing_user.notifications_enabled)

    @responses.activate
    @override_config(
        FEDERATED_IDENTITY_AUTHORITATIVE_ISD="isd:efp",
        FEDERATED_IDENTITY_LOCKED_FIELDS=["first_name", "last_name"],
    )
    def test_locked_fields_not_overwritten_when_authoritative_isd_present(self):
        # EFP is the authoritative source for names of users federated via both
        # EFP and eduTEAMS. The eduTEAMS sync must leave the locked fields
        # (first_name/last_name) intact while still syncing the rest of the
        # profile, avoiding the periodic name flapping. Pre-existing names differ
        # from the eduTEAMS payload ("John Snow") so preservation is observable.
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        remote_user = structure_factories.UserFactory(
            username=self.valid_cuid,
            first_name="Jane",
            last_name="Doe",
            email="foo@example.com",
            active_isds=["isd:efp"],
        )

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        remote_user.refresh_from_db()
        # Locked fields owned by EFP are preserved ...
        self.assertEqual(remote_user.first_name, "Jane")
        self.assertEqual(remote_user.last_name, "Doe")
        # ... while the rest of the eduTEAMS profile still syncs.
        self.assertEqual(remote_user.email, "john@snow.me")
        self.assertIn("isd:efp", remote_user.active_isds)
        self.assertIn("isd:eduteams", remote_user.active_isds)

    @responses.activate
    @override_config(
        FEDERATED_IDENTITY_AUTHORITATIVE_ISD="isd:efp",
        FEDERATED_IDENTITY_LOCKED_FIELDS=["first_name", "last_name"],
    )
    def test_locked_fields_updated_when_authoritative_isd_absent(self):
        # Protection is enabled, but the user is not asserted by the
        # authoritative ISD, so eduTEAMS remains the source of truth.
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        remote_user = structure_factories.UserFactory(
            username=self.valid_cuid,
            first_name="Jane",
            last_name="Doe",
            email="foo@example.com",
        )

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        remote_user.refresh_from_db()
        self.assertEqual(remote_user.first_name, "John")
        self.assertEqual(remote_user.last_name, "Snow")
        self.assertEqual(remote_user.email, "john@snow.me")

    @responses.activate
    def test_locked_fields_updated_when_protection_disabled(self):
        # Default configuration (no authoritative ISD / no locked fields):
        # the protection is off and eduTEAMS updates names as before, even for
        # a user asserted by EFP.
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        remote_user = structure_factories.UserFactory(
            username=self.valid_cuid,
            first_name="Jane",
            last_name="Doe",
            email="foo@example.com",
            active_isds=["isd:efp"],
        )

        response = self.client.post(self.url, {"cuid": self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        remote_user.refresh_from_db()
        self.assertEqual(remote_user.first_name, "John")
        self.assertEqual(remote_user.last_name, "Snow")
