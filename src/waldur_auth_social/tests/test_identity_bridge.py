from unittest.mock import patch

from constance.test import override_config
from django.test import TestCase
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient

from waldur_auth_social.const import WRITABLE_USER_FIELDS
from waldur_auth_social.serializers import (
    POSITIVE_BIG_INTEGER_MAX,
    IdentityBridgeRequestSerializer,
)
from waldur_auth_social.utils import (
    remove_user_from_isd,
    update_user_attributes_from_source,
)
from waldur_core.core.models import User
from waldur_core.core.user_attributes import ALL_PROFILE_ATTRIBUTES
from waldur_core.structure.tests import factories as structure_factories

BRIDGE_URL = reverse("auth_identity_bridge")
BRIDGE_REMOVE_URL = reverse("auth_identity_bridge_remove")
BRIDGE_STATS_URL = reverse("auth_identity_bridge_stats")
BRIDGE_ALLOWED_FIELDS_URL = reverse("auth_identity_bridge_allowed_fields")


class IdentityBridgePermissionTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.valid_payload = {
            "username": "newuser@myaccessid.org",
            "source": "isd:puhuri",
            "first_name": "Alice",
            "last_name": "Smith",
            "email": "alice@example.com",
        }

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_anonymous_returns_401(self):
        response = self.client.post(BRIDGE_URL, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_regular_user_returns_403(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.post(BRIDGE_URL, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_identity_manager_with_matching_isd_returns_200(self):
        user = structure_factories.UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:puhuri"],
        )
        self.client.force_authenticate(user)
        response = self.client.post(BRIDGE_URL, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_identity_manager_with_wrong_isd_returns_403(self):
        user = structure_factories.UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:fenix"],
        )
        self.client.force_authenticate(user)
        response = self.client.post(BRIDGE_URL, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_staff_bypasses_isd_scope_check(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user)
        response = self.client.post(BRIDGE_URL, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=False)
    def test_feature_flag_disabled_returns_403(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user)
        response = self.client.post(BRIDGE_URL, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IdentityBridgeCreateTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_create_new_user(self):
        payload = {
            "username": "newuser@myaccessid.org",
            "source": "isd:puhuri",
            "first_name": "Alice",
            "last_name": "Smith",
            "email": "alice@example.com",
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["created"])

        user = User.objects.get(username="newuser@myaccessid.org")
        self.assertEqual(user.first_name, "Alice")
        self.assertEqual(user.last_name, "Smith")
        self.assertEqual(user.email, "alice@example.com")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.notifications_enabled)
        self.assertEqual(user.registration_method, "isd:puhuri")
        self.assertIn("isd:puhuri", user.active_isds)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_create_sets_attribute_sources(self):
        payload = {
            "username": "newuser@myaccessid.org",
            "source": "isd:fenix",
            "first_name": "Bob",
            "email": "bob@cern.ch",
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user = User.objects.get(username="newuser@myaccessid.org")
        self.assertIn("first_name", user.attribute_sources)
        self.assertEqual(user.attribute_sources["first_name"]["source"], "isd:fenix")
        self.assertIn("email", user.attribute_sources)
        self.assertEqual(user.attribute_sources["email"]["source"], "isd:fenix")


class IdentityBridgeUpdateTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_update_existing_user(self):
        target = structure_factories.UserFactory(
            username="existinguser@myaccessid.org",
            first_name="OldName",
        )
        payload = {
            "username": "existinguser@myaccessid.org",
            "source": "isd:puhuri",
            "first_name": "NewName",
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["created"])
        self.assertIn("first_name", response.data["updated_fields"])

        target.refresh_from_db()
        self.assertEqual(target.first_name, "NewName")

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES=["gender"],
        ENABLED_USER_PROFILE_ATTRIBUTES=["gender"],
    )
    def test_accepts_gender_as_string_choice(self):
        target = structure_factories.UserFactory(
            username="existinguser@myaccessid.org",
            gender=None,
        )
        payload = {
            "username": "existinguser@myaccessid.org",
            "source": "isd:puhuri",
            "gender": "male",
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        target.refresh_from_db()
        self.assertEqual(target.gender, "male")

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_update_deactivated_user_returns_error(self):
        structure_factories.UserFactory(
            username="deactivated@myaccessid.org",
            is_active=False,
        )
        payload = {
            "username": "deactivated@myaccessid.org",
            "source": "isd:puhuri",
            "first_name": "Name",
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SourceTrackingTest(TestCase):
    def test_attribute_sources_set_correctly(self):
        user = structure_factories.UserFactory()
        update_user_attributes_from_source(
            user,
            {"first_name": "Alice", "organization": "CERN"},
            "isd:fenix",
            allowed_fields={"first_name", "organization"},
        )
        user.refresh_from_db()
        self.assertEqual(user.attribute_sources["first_name"]["source"], "isd:fenix")
        self.assertEqual(user.attribute_sources["organization"]["source"], "isd:fenix")
        self.assertIn("timestamp", user.attribute_sources["first_name"])

    def test_untouched_fields_preserved(self):
        user = structure_factories.UserFactory()
        # First update from fenix
        update_user_attributes_from_source(
            user,
            {"first_name": "Alice", "email": "alice@cern.ch"},
            "isd:fenix",
            allowed_fields={"first_name", "email"},
        )
        # Second update from puhuri - only organization
        update_user_attributes_from_source(
            user,
            {"organization": "CSC"},
            "isd:puhuri",
            allowed_fields={"organization"},
        )
        user.refresh_from_db()
        # fenix sources should still be there
        self.assertEqual(user.attribute_sources["first_name"]["source"], "isd:fenix")
        self.assertEqual(user.attribute_sources["email"]["source"], "isd:fenix")
        self.assertEqual(user.attribute_sources["organization"]["source"], "isd:puhuri")


class PreserveOtherSourcesTest(TestCase):
    def test_empty_value_from_non_owner_is_preserved(self):
        """ISD-A sends empty for field owned by ISD-B -> field preserved."""
        user = structure_factories.UserFactory()

        # ISD-B sets email
        update_user_attributes_from_source(
            user,
            {"email": "alice@cern.ch"},
            "isd:puhuri",
            allowed_fields={"email"},
        )
        user.refresh_from_db()
        self.assertEqual(user.email, "alice@cern.ch")

        # ISD-A sends empty email -> should NOT clear it
        update_user_attributes_from_source(
            user,
            {"email": ""},
            "isd:fenix",
            allowed_fields={"email"},
        )
        user.refresh_from_db()
        self.assertEqual(user.email, "alice@cern.ch")  # Preserved!

    def test_empty_value_from_owner_is_cleared(self):
        """ISD-B owns field, ISD-B sends empty -> field cleared."""
        user = structure_factories.UserFactory()

        update_user_attributes_from_source(
            user,
            {"email": "alice@cern.ch"},
            "isd:puhuri",
            allowed_fields={"email"},
        )
        user.refresh_from_db()
        self.assertEqual(user.email, "alice@cern.ch")

        # Same source clears it
        update_user_attributes_from_source(
            user,
            {"email": ""},
            "isd:puhuri",
            allowed_fields={"email"},
        )
        user.refresh_from_db()
        self.assertEqual(user.email, "")

    def test_non_empty_value_transfers_ownership(self):
        """ISD-A owns field, ISD-B sends non-empty -> ownership transfers to B."""
        user = structure_factories.UserFactory()

        update_user_attributes_from_source(
            user,
            {"email": "alice@uni.eu"},
            "isd:fenix",
            allowed_fields={"email"},
        )
        user.refresh_from_db()
        self.assertEqual(user.attribute_sources["email"]["source"], "isd:fenix")

        # ISD-B overwrites
        update_user_attributes_from_source(
            user,
            {"email": "alice@cern.ch"},
            "isd:puhuri",
            allowed_fields={"email"},
        )
        user.refresh_from_db()
        self.assertEqual(user.email, "alice@cern.ch")
        self.assertEqual(user.attribute_sources["email"]["source"], "isd:puhuri")


class LockedFieldsTest(TestCase):
    """Fields locked to an authoritative ISD are protected on the bridge push path."""

    @override_config(
        FEDERATED_IDENTITY_AUTHORITATIVE_ISD="isd:efp",
        FEDERATED_IDENTITY_LOCKED_FIELDS=["first_name", "last_name"],
    )
    def test_locked_field_not_overwritten_by_non_authoritative_source(self):
        user = structure_factories.UserFactory()
        # EFP (authoritative) owns the name.
        update_user_attributes_from_source(
            user,
            {"first_name": "Jane", "last_name": "Doe"},
            "isd:efp",
            allowed_fields={"first_name", "last_name"},
        )
        # A different, non-authoritative bridge source tries to overwrite the
        # locked names and also sends a non-locked field.
        update_user_attributes_from_source(
            user,
            {"first_name": "JANE", "last_name": "DOE", "organization": "CSC"},
            "isd:puhuri",
            allowed_fields={"first_name", "last_name", "organization"},
        )
        user.refresh_from_db()
        # Locked names preserved and still owned by EFP ...
        self.assertEqual(user.first_name, "Jane")
        self.assertEqual(user.last_name, "Doe")
        self.assertEqual(user.attribute_sources["first_name"]["source"], "isd:efp")
        # ... while the non-locked field from the other source still applies.
        self.assertEqual(user.organization, "CSC")
        self.assertEqual(user.attribute_sources["organization"]["source"], "isd:puhuri")

    @override_config(
        FEDERATED_IDENTITY_AUTHORITATIVE_ISD="isd:efp",
        FEDERATED_IDENTITY_LOCKED_FIELDS=["first_name", "last_name"],
    )
    def test_authoritative_source_can_update_locked_field(self):
        user = structure_factories.UserFactory()
        update_user_attributes_from_source(
            user,
            {"first_name": "Jane", "last_name": "Doe"},
            "isd:efp",
            allowed_fields={"first_name", "last_name"},
        )
        # EFP itself updates the locked field — the lock must not block the owner.
        update_user_attributes_from_source(
            user,
            {"first_name": "Mary"},
            "isd:efp",
            allowed_fields={"first_name"},
        )
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Mary")

    def test_locked_field_overwritten_when_protection_disabled(self):
        # Default configuration: no authoritative ISD -> existing last-writer-wins.
        user = structure_factories.UserFactory()
        update_user_attributes_from_source(
            user,
            {"first_name": "Jane"},
            "isd:efp",
            allowed_fields={"first_name"},
        )
        update_user_attributes_from_source(
            user,
            {"first_name": "JANE"},
            "isd:puhuri",
            allowed_fields={"first_name"},
        )
        user.refresh_from_db()
        self.assertEqual(user.first_name, "JANE")


class MultiISDDeactivationTest(TestCase):
    @override_config(FEDERATED_IDENTITY_DEACTIVATION_POLICY="all_isds_removed")
    def test_remove_from_one_isd_keeps_active(self):
        user = structure_factories.UserFactory(
            active_isds=["isd:fenix", "isd:puhuri"],
            attribute_sources={
                "first_name": {
                    "source": "isd:fenix",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                "email": {"source": "isd:puhuri", "timestamp": "2026-01-01T00:00:00Z"},
            },
        )
        deactivated = remove_user_from_isd(user, "isd:fenix")
        user.refresh_from_db()

        self.assertFalse(deactivated)
        self.assertTrue(user.is_active)
        self.assertEqual(user.active_isds, ["isd:puhuri"])
        # fenix-owned attribute cleared
        self.assertNotIn("first_name", user.attribute_sources)
        # puhuri-owned attribute preserved
        self.assertIn("email", user.attribute_sources)

    @override_config(FEDERATED_IDENTITY_DEACTIVATION_POLICY="all_isds_removed")
    def test_remove_from_all_isds_deactivates(self):
        user = structure_factories.UserFactory(
            active_isds=["isd:puhuri"],
            attribute_sources={
                "email": {"source": "isd:puhuri", "timestamp": "2026-01-01T00:00:00Z"},
            },
        )
        deactivated = remove_user_from_isd(user, "isd:puhuri")
        user.refresh_from_db()

        self.assertTrue(deactivated)
        self.assertFalse(user.is_active)
        self.assertEqual(user.active_isds, [])

    @override_config(FEDERATED_IDENTITY_DEACTIVATION_POLICY="all_isds_removed")
    def test_remove_from_all_isds_sets_deactivation_reason(self):
        user = structure_factories.UserFactory(
            active_isds=["isd:puhuri"],
            attribute_sources={
                "email": {"source": "isd:puhuri", "timestamp": "2026-01-01T00:00:00Z"},
            },
        )
        remove_user_from_isd(user, "isd:puhuri")
        user.refresh_from_db()

        self.assertEqual(
            user.deactivation_reason,
            "Identity source 'isd:puhuri' removed (policy: all_isds_removed)",
        )

    @override_config(FEDERATED_IDENTITY_DEACTIVATION_POLICY="any_isd_removed")
    def test_any_isd_removed_policy_deactivates_immediately(self):
        user = structure_factories.UserFactory(
            active_isds=["isd:fenix", "isd:puhuri"],
        )
        deactivated = remove_user_from_isd(user, "isd:fenix")
        user.refresh_from_db()

        self.assertTrue(deactivated)
        self.assertFalse(user.is_active)

    @override_config(FEDERATED_IDENTITY_DEACTIVATION_POLICY="any_isd_removed")
    def test_any_isd_removed_sets_deactivation_reason(self):
        user = structure_factories.UserFactory(
            active_isds=["isd:fenix", "isd:puhuri"],
        )
        remove_user_from_isd(user, "isd:fenix")
        user.refresh_from_db()

        self.assertEqual(
            user.deactivation_reason,
            "Identity source 'isd:fenix' removed (policy: any_isd_removed)",
        )

    @override_config(FEDERATED_IDENTITY_DEACTIVATION_POLICY="all_isds_removed")
    def test_remove_from_one_isd_does_not_set_deactivation_reason(self):
        user = structure_factories.UserFactory(
            active_isds=["isd:fenix", "isd:puhuri"],
            attribute_sources={
                "first_name": {
                    "source": "isd:fenix",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                "email": {"source": "isd:puhuri", "timestamp": "2026-01-01T00:00:00Z"},
            },
        )
        remove_user_from_isd(user, "isd:fenix")
        user.refresh_from_db()

        self.assertTrue(user.is_active)
        self.assertEqual(user.deactivation_reason, "")


class IdentityBridgeSourceValidationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_invalid_source_format_rejected(self):
        payload = {
            "username": "user@test.org",
            "source": "invalid-source",
            "first_name": "Test",
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES=["first_name", "last_name", "email"],
    )
    def test_disallowed_field_rejected(self):
        payload = {
            "username": "user@test.org",
            "source": "isd:puhuri",
            "organization": "CERN",  # Not in allowed list
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ActiveIsdsTrackingTest(TestCase):
    def test_source_added_to_active_isds(self):
        user = structure_factories.UserFactory(active_isds=[])
        update_user_attributes_from_source(
            user,
            {"first_name": "Alice"},
            "isd:fenix",
            allowed_fields={"first_name"},
        )
        user.refresh_from_db()
        self.assertIn("isd:fenix", user.active_isds)

    def test_duplicate_source_not_added(self):
        user = structure_factories.UserFactory(active_isds=["isd:fenix"])
        update_user_attributes_from_source(
            user,
            {"first_name": "Alice"},
            "isd:fenix",
            allowed_fields={"first_name"},
        )
        user.refresh_from_db()
        self.assertEqual(user.active_isds.count("isd:fenix"), 1)


class AuditLoggingTest(TestCase):
    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    @patch("waldur_core.logging.event_logger.emit")
    def test_audit_log_contains_source(self, mock_emit):
        user = structure_factories.UserFactory(
            username="audituser@myaccessid.org",
            first_name="Old",
        )
        update_user_attributes_from_source(
            user,
            {"first_name": "New"},
            "isd:puhuri",
            allowed_fields={"first_name"},
        )
        # Check that at least one emit call mentions the source
        found_source = False
        for call in mock_emit.call_args_list:
            msg = call[0][0] if call[0] else ""
            if "isd:puhuri" in msg:
                found_source = True
                break
        self.assertTrue(found_source, "Audit log should contain the change source")


class IdentityBridgeRemovePermissionTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.valid_payload = {
            "username": "user@myaccessid.org",
            "source": "isd:puhuri",
        }

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_anonymous_returns_401(self):
        response = self.client.post(
            BRIDGE_REMOVE_URL, self.valid_payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_regular_user_returns_403(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.post(
            BRIDGE_REMOVE_URL, self.valid_payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=False)
    def test_feature_flag_disabled_returns_403(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user)
        response = self.client.post(
            BRIDGE_REMOVE_URL, self.valid_payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_identity_manager_with_wrong_isd_returns_403(self):
        user = structure_factories.UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:fenix"],
        )
        self.client.force_authenticate(user)
        # target user must exist
        structure_factories.UserFactory(
            username="user@myaccessid.org",
            active_isds=["isd:puhuri"],
        )
        response = self.client.post(
            BRIDGE_REMOVE_URL, self.valid_payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IdentityBridgeRemoveTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_DEACTIVATION_POLICY="all_isds_removed",
    )
    def test_remove_user_from_isd(self):
        target = structure_factories.UserFactory(
            username="user@myaccessid.org",
            active_isds=["isd:puhuri", "isd:fenix"],
            attribute_sources={
                "first_name": {
                    "source": "isd:puhuri",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                "email": {"source": "isd:fenix", "timestamp": "2026-01-01T00:00:00Z"},
            },
        )
        payload = {"username": "user@myaccessid.org", "source": "isd:puhuri"}
        response = self.client.post(BRIDGE_REMOVE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["deactivated"])

        target.refresh_from_db()
        self.assertTrue(target.is_active)
        self.assertEqual(target.active_isds, ["isd:fenix"])
        self.assertNotIn("first_name", target.attribute_sources)
        self.assertIn("email", target.attribute_sources)

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_DEACTIVATION_POLICY="all_isds_removed",
    )
    def test_remove_from_last_isd_deactivates(self):
        target = structure_factories.UserFactory(
            username="user@myaccessid.org",
            active_isds=["isd:puhuri"],
        )
        payload = {"username": "user@myaccessid.org", "source": "isd:puhuri"}
        response = self.client.post(BRIDGE_REMOVE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["deactivated"])

        target.refresh_from_db()
        self.assertFalse(target.is_active)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_remove_nonexistent_user_returns_404(self):
        payload = {"username": "nonexistent@myaccessid.org", "source": "isd:puhuri"}
        response = self.client.post(BRIDGE_REMOVE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_invalid_source_format_rejected(self):
        structure_factories.UserFactory(username="user@myaccessid.org")
        payload = {"username": "user@myaccessid.org", "source": "bad-format"}
        response = self.client.post(BRIDGE_REMOVE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_identity_manager_with_matching_isd_can_remove(self):
        manager = structure_factories.UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:puhuri"],
        )
        structure_factories.UserFactory(
            username="user@myaccessid.org",
            active_isds=["isd:puhuri"],
        )
        self.client.force_authenticate(manager)
        payload = {"username": "user@myaccessid.org", "source": "isd:puhuri"}
        response = self.client.post(BRIDGE_REMOVE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["deactivated"])


class UserSerializerIdentityBridgeFieldsTest(TestCase):
    """Test that identity bridge fields are exposed to staff on /api/users/."""

    def setUp(self):
        self.client = APIClient()

    def test_staff_sees_identity_bridge_fields(self):
        target = structure_factories.UserFactory(
            active_isds=["isd:fenix"],
            managed_isds=["isd:puhuri"],
            attribute_sources={
                "email": {"source": "isd:fenix", "timestamp": "2026-01-01T00:00:00Z"}
            },
        )
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        url = reverse("user-detail", kwargs={"uuid": target.uuid.hex})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("active_isds", response.data)
        self.assertIn("managed_isds", response.data)
        self.assertIn("attribute_sources", response.data)
        self.assertIn("is_identity_manager", response.data)
        self.assertEqual(response.data["active_isds"], ["isd:fenix"])
        self.assertEqual(response.data["managed_isds"], ["isd:puhuri"])

    def test_regular_user_sees_own_identity_bridge_fields(self):
        """Regular users can see identity bridge fields on their own profile."""
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        url = reverse("user-detail", kwargs={"uuid": user.uuid.hex})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("active_isds", response.data)
        self.assertIn("managed_isds", response.data)
        self.assertIn("is_identity_manager", response.data)
        # attribute_sources is staff-only
        self.assertNotIn("attribute_sources", response.data)

    def test_support_does_not_see_identity_bridge_fields_on_other_user(self):
        """Support users cannot see identity bridge fields on other users."""
        support = structure_factories.UserFactory(is_support=True)
        other = structure_factories.UserFactory(
            active_isds=["isd:fenix"],
            managed_isds=["isd:puhuri"],
        )
        self.client.force_authenticate(support)
        url = reverse("user-detail", kwargs={"uuid": other.uuid.hex})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("active_isds", response.data)
        self.assertNotIn("managed_isds", response.data)
        self.assertNotIn("attribute_sources", response.data)
        self.assertNotIn("is_identity_manager", response.data)


class IdentityBridgeUserStatusTest(TestCase):
    """Test GET /api/users/{uuid}/identity_bridge_status/."""

    def setUp(self):
        self.client = APIClient()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def test_returns_status_for_federated_user(self):
        target = structure_factories.UserFactory(
            active_isds=["isd:fenix", "isd:puhuri"],
            managed_isds=[],
            attribute_sources={
                "email": {
                    "source": "isd:fenix",
                    "timestamp": "2026-02-05T12:00:00+00:00",
                },
                "organization": {
                    "source": "isd:puhuri",
                    "timestamp": "2026-02-05T12:00:00+00:00",
                },
            },
        )
        url = reverse("user-detail", kwargs={"uuid": target.uuid.hex})
        response = self.client.get(f"{url}identity_bridge_status/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["active_isds"], ["isd:fenix", "isd:puhuri"])
        self.assertIn("email", response.data["attribute_sources"])
        self.assertEqual(
            response.data["attribute_sources"]["email"]["source"], "isd:fenix"
        )
        self.assertTrue(response.data["is_federated"])
        self.assertIsInstance(response.data["effective_bridge_fields"], list)

    def test_detects_stale_attributes(self):
        target = structure_factories.UserFactory(
            active_isds=["isd:fenix"],
            attribute_sources={
                "email": {
                    "source": "isd:fenix",
                    "timestamp": "2020-01-01T00:00:00+00:00",
                },
            },
        )
        url = reverse("user-detail", kwargs={"uuid": target.uuid.hex})
        response = self.client.get(f"{url}identity_bridge_status/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("email", response.data["stale_attributes"])
        self.assertTrue(response.data["attribute_sources"]["email"]["is_stale"])

    def test_non_federated_user_returns_empty(self):
        target = structure_factories.UserFactory(
            active_isds=[],
            attribute_sources={},
        )
        url = reverse("user-detail", kwargs={"uuid": target.uuid.hex})
        response = self.client.get(f"{url}identity_bridge_status/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_federated"])
        self.assertEqual(response.data["active_isds"], [])
        self.assertEqual(response.data["attribute_sources"], {})

    def test_regular_user_forbidden(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        url = reverse("user-detail", kwargs={"uuid": user.uuid.hex})
        response = self.client.get(f"{url}identity_bridge_status/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IdentityBridgeStatsTest(TestCase):
    """Test GET /api/identity-bridge/stats/."""

    def setUp(self):
        self.client = APIClient()

    def test_staff_can_view_stats(self):
        structure_factories.UserFactory(
            active_isds=["isd:fenix"],
            attribute_sources={
                "email": {
                    "source": "isd:fenix",
                    "timestamp": "2026-02-05T12:00:00+00:00",
                },
            },
        )
        structure_factories.UserFactory(
            active_isds=["isd:fenix", "isd:puhuri"],
            attribute_sources={
                "email": {
                    "source": "isd:puhuri",
                    "timestamp": "2026-02-05T12:00:00+00:00",
                },
            },
        )
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(BRIDGE_STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("enabled", response.data)
        self.assertIn("users_per_isd", response.data)
        self.assertIn("total_federated_users", response.data)
        self.assertEqual(response.data["total_federated_users"], 2)

        # Check per-ISD data
        isd_names = [isd["isd"] for isd in response.data["users_per_isd"]]
        self.assertIn("isd:fenix", isd_names)
        self.assertIn("isd:puhuri", isd_names)

        fenix = next(
            i for i in response.data["users_per_isd"] if i["isd"] == "isd:fenix"
        )
        self.assertEqual(fenix["user_count"], 2)

    def test_regular_user_forbidden(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(BRIDGE_STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_returns_401(self):
        response = self.client.get(BRIDGE_STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=False)
    def test_reports_disabled_state(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(BRIDGE_STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["enabled"])


class LegacySourceMigrationTest(TestCase):
    """Test that create_or_update_oauth_user populates attribute_sources."""

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_oauth_user_creation_sets_attribute_sources(self):
        from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
        from waldur_auth_social.models import IdentityProvider
        from waldur_auth_social.utils import create_or_update_oauth_user

        idp = IdentityProvider(
            provider=ProviderChoices.REMOTE_EDUTEAMS,
            **PROVIDER_DEFAULTS[ProviderChoices.REMOTE_EDUTEAMS],
        )
        backend_user = {
            "voperson_id": "testcuid@myaccessid.org",
            "given_name": "Test",
            "family_name": "User",
            "mail": "test@example.com",
        }
        user, created = create_or_update_oauth_user(idp, backend_user)

        self.assertTrue(created)
        self.assertIn("first_name", user.attribute_sources)
        self.assertEqual(user.attribute_sources["first_name"]["source"], "isd:eduteams")
        self.assertIn("isd:eduteams", user.active_isds)


class IdentityBridgeAllowedFieldsTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_anonymous_returns_401(self):
        response = self.client.get(BRIDGE_ALLOWED_FIELDS_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_regular_user_returns_403(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(BRIDGE_ALLOWED_FIELDS_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=False)
    def test_feature_flag_disabled_returns_403(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user)
        response = self.client.get(BRIDGE_ALLOWED_FIELDS_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES=["first_name", "last_name", "email"],
    )
    def test_staff_gets_allowed_fields(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user)
        response = self.client.get(BRIDGE_ALLOWED_FIELDS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("allowed_fields", response.data)
        # The result is the three-way intersection, so at minimum these should be present
        # (assuming they're also in WRITABLE_USER_FIELDS and enabled profile attributes)
        for field in ["email", "first_name", "last_name"]:
            self.assertIn(field, response.data["allowed_fields"])

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES=["first_name", "last_name", "email"],
    )
    def test_identity_manager_gets_allowed_fields(self):
        user = structure_factories.UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:puhuri"],
        )
        self.client.force_authenticate(user)
        response = self.client.get(BRIDGE_ALLOWED_FIELDS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("allowed_fields", response.data)


ALL_ATTRIBUTES = list(ALL_PROFILE_ATTRIBUTES)


class IdentityBridgeSerializerCoverageTest(TestCase):
    """Guard against drift between the writable whitelist and the bridge serializer.

    An undeclared field never reaches validated_data, so it is dropped without
    an error. Pin the two lists so the drift is caught in CI, not in production.
    """

    # Fields deliberately not settable over the bridge. Keep empty unless there
    # is a documented reason, and state it here.
    EXCLUDED_FROM_BRIDGE: set[str] = set()

    def test_every_writable_field_is_declared_on_the_serializer(self):
        declared = set(IdentityBridgeRequestSerializer().fields)
        missing = set(WRITABLE_USER_FIELDS) - declared - self.EXCLUDED_FROM_BRIDGE
        self.assertEqual(
            missing,
            set(),
            f"Fields in WRITABLE_USER_FIELDS are not declared on "
            f"IdentityBridgeRequestSerializer and would be silently dropped: "
            f"{', '.join(sorted(missing))}",
        )

    def test_serializer_declares_no_field_outside_the_whitelist(self):
        declared = set(IdentityBridgeRequestSerializer().fields) - {
            "username",
            "source",
        }
        self.assertEqual(declared - set(WRITABLE_USER_FIELDS), set())


@override_config(
    FEDERATED_IDENTITY_SYNC_ENABLED=True,
    FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES=ALL_ATTRIBUTES,
    ENABLED_USER_PROFILE_ATTRIBUTES=ALL_ATTRIBUTES,
)
class IdentityBridgePosixAndOrganizationFieldsTest(TestCase):
    """POSIX and organization registry attributes must survive the round trip."""

    def setUp(self):
        self.client = APIClient()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.payload = {
            "username": "posix@myaccessid.org",
            "source": "isd:puhuri",
            "uid_number": 100000,
            "primary_gid": 200000,
            "organization_registry_code": "12345678",
            "organization_vat_code": "EE123456789",
            "organization_address": "Narva mnt 18, Tartu",
        }
        self.fields = [k for k in self.payload if k not in ("username", "source")]

    def test_create_persists_fields(self):
        response = self.client.post(BRIDGE_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["created"])

        user = User.objects.get(username="posix@myaccessid.org")
        for field in self.fields:
            self.assertEqual(getattr(user, field), self.payload[field])

    def test_create_reports_fields_as_updated(self):
        response = self.client.post(BRIDGE_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.fields:
            self.assertIn(field, response.data["updated_fields"])

    def test_create_records_attribute_sources(self):
        self.client.post(BRIDGE_URL, self.payload, format="json")

        user = User.objects.get(username="posix@myaccessid.org")
        for field in self.fields:
            self.assertIn(field, user.attribute_sources)
            self.assertEqual(user.attribute_sources[field]["source"], "isd:puhuri")
            self.assertIn("timestamp", user.attribute_sources[field])

    def test_update_persists_fields(self):
        target = structure_factories.UserFactory(username="posix@myaccessid.org")
        response = self.client.post(BRIDGE_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["created"])

        target.refresh_from_db()
        for field in self.fields:
            self.assertEqual(getattr(target, field), self.payload[field])
            self.assertIn(field, response.data["updated_fields"])
            self.assertEqual(target.attribute_sources[field]["source"], "isd:puhuri")

    def test_null_posix_ids_are_accepted(self):
        payload = dict(self.payload, uid_number=None, primary_gid=None)
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user = User.objects.get(username="posix@myaccessid.org")
        self.assertIsNone(user.uid_number)
        self.assertIsNone(user.primary_gid)

    def test_negative_uid_number_rejected(self):
        payload = dict(self.payload, uid_number=-1)
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("uid_number", response.data)
        self.assertFalse(User.objects.filter(username="posix@myaccessid.org").exists())

    def test_out_of_range_primary_gid_rejected(self):
        payload = dict(self.payload, primary_gid=POSITIVE_BIG_INTEGER_MAX + 1)
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("primary_gid", response.data)
        self.assertFalse(User.objects.filter(username="posix@myaccessid.org").exists())


class IdentityBridgeAllowListStillAppliesTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES=["first_name"],
        ENABLED_USER_PROFILE_ATTRIBUTES=["first_name"],
    )
    def test_uid_number_rejected_when_not_in_allowed_attributes(self):
        payload = {
            "username": "posix@myaccessid.org",
            "source": "isd:puhuri",
            "uid_number": 100000,
        }
        response = self.client.post(BRIDGE_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(username="posix@myaccessid.org").exists())

    @override_config(
        FEDERATED_IDENTITY_SYNC_ENABLED=True,
        FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES=["uid_number"],
        ENABLED_USER_PROFILE_ATTRIBUTES=["uid_number"],
    )
    def test_allowed_but_undeclared_field_is_reported_not_dropped(self):
        """A configuration-allowed field the serializer forgot must fail loudly."""

        class SerializerMissingUidNumber(IdentityBridgeRequestSerializer):
            def get_fields(self):
                fields = super().get_fields()
                fields.pop("uid_number")
                return fields

        serializer = SerializerMissingUidNumber(
            data={
                "username": "posix@myaccessid.org",
                "source": "isd:puhuri",
                "uid_number": 100000,
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("uid_number", str(serializer.errors))
