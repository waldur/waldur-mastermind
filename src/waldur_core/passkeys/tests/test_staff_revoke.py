"""Staff revoke is the recovery path for a lost authenticator.

There are deliberately no backup codes — they would reintroduce a phishable
factor — so this and "hold more than one credential" are the whole recovery
story. That makes the audit trail load-bearing: the affected user has to be
able to see who took their credential away and why.
"""

from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.passkeys.tests.factories import PasskeyCredentialFactory
from waldur_core.passkeys.tests.helpers import enable_passkeys
from waldur_core.structure.tests import factories as structure_factories

LIST_URL = "/api/staff-passkeys/"


def revoke_url(credential):
    return f"{LIST_URL}{credential.uuid.hex}/revoke/"


@enable_passkeys()
class StaffPasskeyRevokeTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = structure_factories.UserFactory()
        self.credential = PasskeyCredentialFactory(user=self.owner)
        self.client.force_authenticate(self.staff)

    def test_staff_can_revoke_another_users_passkey(self):
        response = self.client.post(
            revoke_url(self.credential), {"reason": "Laptop was stolen"}
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.credential.refresh_from_db()
        self.assertFalse(self.credential.is_active)
        self.assertEqual(self.credential.revoked_by, self.staff)
        self.assertEqual(self.credential.revocation_reason, "Laptop was stolen")

    def test_the_reason_is_mandatory(self):
        response = self.client.post(revoke_url(self.credential), {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.credential.refresh_from_db()
        self.assertTrue(self.credential.is_active)

    def test_a_blank_reason_is_refused(self):
        response = self.client.post(revoke_url(self.credential), {"reason": "   "})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_event_names_the_affected_user_not_the_actor(self):
        """The person who lost the authenticator is who this is about."""
        self.client.post(revoke_url(self.credential), {"reason": "Lost device"})

        event = logging_models.Event.objects.filter(
            event_type=EventType.PASSKEY_REVOKED_BY_STAFF
        ).latest("created")

        self.assertEqual(event.context["affected_user_username"], self.owner.username)
        self.assertIn("Lost device", event.message)

    def test_the_affected_user_can_see_it_in_their_own_audit_log(self):
        self.client.post(revoke_url(self.credential), {"reason": "Lost device"})

        self.client.force_authenticate(self.owner)
        response = self.client.get(
            "/api/events/",
            {
                "scope": f"http://testserver/api/users/{self.owner.uuid.hex}/",
                "feature": "users",
            },
        )

        types = [row["event_type"] for row in response.data]
        self.assertIn(EventType.PASSKEY_REVOKED_BY_STAFF.value, types)

    def test_a_non_staff_user_cannot(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.post(revoke_url(self.credential), {"reason": "Nope"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_do_not_see_their_own_credentials_here(self):
        """Self-management belongs to /api/passkeys/, which needs no reason."""
        own = PasskeyCredentialFactory(user=self.staff)

        response = self.client.get(LIST_URL)

        uuids = [row["uuid"] for row in response.data]
        self.assertNotIn(own.uuid.hex, uuids)
        self.assertIn(self.credential.uuid.hex, uuids)

    def test_the_list_can_be_narrowed_to_one_user(self):
        other = PasskeyCredentialFactory()

        response = self.client.get(LIST_URL, {"user_uuid": self.owner.uuid.hex})

        uuids = [row["uuid"] for row in response.data]
        self.assertEqual(uuids, [self.credential.uuid.hex])
        self.assertNotIn(other.uuid.hex, uuids)

    def test_revoking_twice_is_refused(self):
        self.client.post(revoke_url(self.credential), {"reason": "First"})
        response = self.client.post(revoke_url(self.credential), {"reason": "Second"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@enable_passkeys()
class ImpersonationCannotRevokeTest(test.APITestCase):
    """An impersonator acts as somebody else, so the audit trail would name
    the wrong person for the one action that must name the right one."""

    def test_revoking_while_impersonating_is_refused(self):
        staff = structure_factories.UserFactory(is_staff=True)
        victim = structure_factories.UserFactory(is_staff=True)
        credential = PasskeyCredentialFactory()
        Token.objects.get_or_create(user=staff)
        Token.objects.get_or_create(user=victim)

        response = self.client.post(
            revoke_url(credential),
            {"reason": "Sneaky"},
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=staff).key}",
            HTTP_X_IMPERSONATED_USER_UUID=victim.uuid.hex,
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        credential.refresh_from_db()
        self.assertTrue(credential.is_active)
