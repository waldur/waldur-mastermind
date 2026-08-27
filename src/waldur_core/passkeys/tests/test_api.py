from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.passkeys.models import PasskeyCredential
from waldur_core.passkeys.tests.factories import PasskeyCredentialFactory
from waldur_core.passkeys.tests.helpers import enable_passkeys
from waldur_core.structure.tests import factories as structure_factories

LIST_URL = "/api/passkeys/"


def detail_url(credential):
    return f"{LIST_URL}{credential.uuid.hex}/"


@enable_passkeys()
class PasskeyListTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other = structure_factories.UserFactory()
        self.credential = PasskeyCredentialFactory(user=self.user)
        self.foreign = PasskeyCredentialFactory(user=self.other)

    def test_anonymous_access_is_refused(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_sees_only_their_own_credentials(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [row["uuid"] for row in response.data]
        self.assertEqual(uuids, [self.credential.uuid.hex])

    def test_user_cannot_retrieve_another_users_credential(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(detail_url(self.foreign))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_do_not_get_a_back_door_through_this_endpoint(self):
        """Staff revoke is a separate, audited path — not implicit list access."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.data, [])

    def test_no_secret_material_is_exposed(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(detail_url(self.credential))
        self.assertNotIn("public_key", response.data)
        self.assertNotIn("credential_id", response.data)


@enable_passkeys()
class PasskeyRenameTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.credential = PasskeyCredentialFactory(user=self.user)
        self.client.force_authenticate(self.user)

    def test_user_can_rename_their_passkey(self):
        response = self.client.patch(detail_url(self.credential), {"name": "Yubikey 5"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.credential.refresh_from_db()
        self.assertEqual(self.credential.name, "Yubikey 5")

    def test_user_cannot_rename_another_users_passkey(self):
        foreign = PasskeyCredentialFactory()
        response = self.client.patch(detail_url(foreign), {"name": "Mine now"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@enable_passkeys()
class PasskeyRevokeTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.credential = PasskeyCredentialFactory(user=self.user)
        self.client.force_authenticate(self.user)

    def test_revoke_soft_deletes_and_keeps_the_record(self):
        response = self.client.post(
            f"{detail_url(self.credential)}revoke/", {"reason": "Lost the key"}
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.credential.refresh_from_db()
        self.assertFalse(self.credential.is_active)
        self.assertIsNotNone(self.credential.revoked_at)
        self.assertEqual(self.credential.revoked_by, self.user)
        self.assertEqual(self.credential.revocation_reason, "Lost the key")
        # The row survives, so the audit trail does too.
        self.assertTrue(
            PasskeyCredential.objects.filter(pk=self.credential.pk).exists()
        )

    def test_revoking_twice_is_refused(self):
        self.client.post(f"{detail_url(self.credential)}revoke/", {})
        response = self.client.post(f"{detail_url(self.credential)}revoke/", {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_revoke_another_users_passkey(self):
        foreign = PasskeyCredentialFactory()
        response = self.client.post(f"{detail_url(foreign)}revoke/", {})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        foreign.refresh_from_db()
        self.assertTrue(foreign.is_active)


class PasskeysDisabledTest(test.APITestCase):
    """With passkeys off — the default — the API is not reachable at all."""

    @override_waldur_core_settings(AUTHENTICATION_METHODS=["LOCAL_SIGNIN"])
    def test_endpoint_is_hidden_when_disabled(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@enable_passkeys()
class UserSerializerPasskeyFieldsTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(self.user)

    def test_user_without_a_passkey_reports_zero(self):
        response = self.client.get("/api/users/me/")
        self.assertFalse(response.data["has_passkey"])
        self.assertEqual(response.data["passkey_count"], 0)

    def test_active_credentials_are_counted(self):
        PasskeyCredentialFactory(user=self.user)
        PasskeyCredentialFactory(user=self.user)

        response = self.client.get("/api/users/me/")

        self.assertTrue(response.data["has_passkey"])
        self.assertEqual(response.data["passkey_count"], 2)

    def test_revoked_credentials_are_not_counted(self):
        PasskeyCredentialFactory(user=self.user).revoke()

        response = self.client.get("/api/users/me/")

        self.assertFalse(response.data["has_passkey"])
        self.assertEqual(response.data["passkey_count"], 0)

    def test_user_list_does_not_scale_queries_with_the_number_of_users(self):
        """The count is annotated, not fetched per row.

        Without the annotation this endpoint issues one extra query per user in
        the page, so the absolute number matters less than the fact that it
        does not move when the page gets bigger.
        """
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        for _ in range(2):
            PasskeyCredentialFactory(user=structure_factories.UserFactory())

        # Warm up: the first call lazily materialises a Constance default,
        # which would otherwise show up as a difference between the two runs.
        self.client.get("/api/users/")

        with CaptureQueriesContext(connection) as small:
            self.client.get("/api/users/")

        for _ in range(5):
            PasskeyCredentialFactory(user=structure_factories.UserFactory())

        with CaptureQueriesContext(connection) as large:
            response = self.client.get("/api/users/")

        # Guard the guard: if pagination clipped the page back to its old size
        # the comparison below would pass for the wrong reason.
        self.assertGreater(len(response.data), 5)
        self.assertEqual(len(large), len(small))

    def test_user_list_annotates_the_count_in_a_single_query(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        PasskeyCredentialFactory(user=self.user)

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/users/")

        counts = {row["username"]: row["passkey_count"] for row in response.data}
        self.assertEqual(counts[self.user.username], 1)
        self.assertEqual(counts[staff.username], 0)

        annotating = [
            q for q in queries.captured_queries if "active_passkey_count" in q["sql"]
        ]
        self.assertEqual(len(annotating), 1)


class UserSerializerPasskeysDisabledTest(test.APITestCase):
    @override_waldur_core_settings(AUTHENTICATION_METHODS=["LOCAL_SIGNIN"])
    def test_count_is_zero_without_touching_the_database(self):
        user = structure_factories.UserFactory()
        PasskeyCredentialFactory(user=user)
        self.client.force_authenticate(user)

        response = self.client.get("/api/users/me/")

        self.assertFalse(response.data["has_passkey"])
        self.assertEqual(response.data["passkey_count"], 0)
