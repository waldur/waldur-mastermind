import uuid

from constance.test.unittest import override_config as override_constance_config
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.utils import IntegrityError
from django.test import RequestFactory
from django.test.utils import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
)
from waldur_core.structure.tests.factories import (
    CustomerFactory,
    ProjectFactory,
    UserFactory,
)
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests.factories import (
    CategoryFactory,
    OfferingComponentFactory,
    OfferingFactory,
    OrderFactory,
    PlanFactory,
    ResourceFactory,
    ServiceProviderFactory,
)

User = get_user_model()


def deactivate_tos_config(tos_config):
    tos_config.is_active = False
    tos_config.save()


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class TermsOfServiceConsentTest(APITransactionTestCase):
    def setUp(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_RESOURCES)

        self.user = UserFactory()
        self.customer = CustomerFactory()
        self.project = ProjectFactory(customer=self.customer)
        self.category = CategoryFactory()
        self.offering = OfferingFactory(
            category=self.category,
            customer=self.customer,
            type="Marketplace.Basic",
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
            },
        )

        self.tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test Terms of Service",
            version="1.0",
            is_active=True,
        )

        self.plan = PlanFactory(offering=self.offering)
        self.resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        # Set resource to OK state to enable offering user creation
        self.resource.state = ResourceStates.OK
        self.resource.save()

        # Add user to project
        self.project.add_user(self.user, role=ProjectRole.MANAGER)

        self.consent_list_url = reverse("marketplace-user-offering-consent-list")
        self.consent_data = {
            "offering": self.offering.uuid,
        }

    def test_grant_consent(self):
        """Test granting consent to Terms of Service."""
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.consent_list_url, self.consent_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        consent = models.UserOfferingConsent.objects.get(
            user=self.user, offering=self.offering
        )
        self.assertIsNone(consent.revocation_date)
        self.assertEqual(consent.version, "1.0")

    def test_revoke_consent(self):
        """Test revoking consent to Terms of Service."""
        self.client.force_authenticate(user=self.user)

        # First grant consent
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        url = reverse(
            "marketplace-user-offering-consent-revoke",
            kwargs={"uuid": consent.uuid},
        )

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify consent was revoked
        consent.refresh_from_db()
        self.assertIsNotNone(consent.revocation_date)

    def test_only_one_active_tos_config_is_allowed(self):
        """Test that only one active ToS config is allowed."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test Terms of Service",
            version="1.0",
        )
        with self.assertRaises(IntegrityError):
            models.OfferingTermsOfService.objects.create(
                offering=self.offering,
                terms_of_service="Test Terms of Service",
                version="1.0",
                is_active=True,
            )

    def test_consent_status_fields_in_serializer(self):
        """Test that consent status fields are included in serializer response."""
        self.client.force_authenticate(user=self.user)

        # Create consent
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Get consent via list endpoint with filtering
        response = self.client.get(
            f"{self.consent_list_url}?offering_uuid={self.offering.uuid}&user_uuid={self.user.uuid}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        consent_data = response.data[0]
        self.assertTrue(consent_data["has_consent"])
        self.assertEqual(consent_data["version"], "1.0")
        self.assertFalse(consent_data["requires_reconsent"])

    def test_consent_status_without_consent(self):
        """Test consent status when user has not consented."""
        self.client.force_authenticate(user=self.user)

        # Get consent via list endpoint with filtering
        response = self.client.get(
            f"{self.consent_list_url}?offering_uuid={self.offering.uuid}&user_uuid={self.user.uuid}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)  # No consents found

    def test_consent_status_requires_reconsent(self):
        """Test consent status when reconsent is required."""
        self.client.force_authenticate(user=self.user)

        # Deactivate the existing ToS config
        self.tos_config.is_active = False
        self.tos_config.save()

        # Create new ToS config that requires reconsent
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Updated Terms of Service",
            version="2.0",
            requires_reconsent=True,
            is_active=True,
        )

        # Create old consent
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Get consent via list endpoint with filtering
        response = self.client.get(
            f"{self.consent_list_url}?offering_uuid={self.offering.uuid}&user_uuid={self.user.uuid}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        consent_data = response.data[0]
        self.assertTrue(consent_data["has_consent"])
        self.assertEqual(consent_data["version"], "1.0")
        self.assertTrue(consent_data["requires_reconsent"])

    def test_list_consents(self):
        """Test listing user consents."""
        self.client.force_authenticate(user=self.user)

        # Create consent
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        url = reverse("marketplace-user-offering-consent-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(consent.uuid))

    def test_list_consents_filtered_by_user(self):
        """Test listing consents filtered by user."""
        self.client.force_authenticate(user=self.user)

        # Create another user and consent
        other_user = UserFactory()
        other_consent = models.UserOfferingConsent.objects.create(
            user=other_user,
            offering=self.offering,
            version="1.0",
        )

        # Create consent for current user
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        response = self.client.get(self.consent_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only see current user's consents
        consent_uuids = [c["uuid"] for c in response.data]
        self.assertIn(str(consent.uuid), consent_uuids)
        self.assertNotIn(str(other_consent.uuid), consent_uuids)

    def test_consent_permissions(self):
        """Test that users can only access their own consents."""
        other_user = UserFactory()
        self.client.force_authenticate(user=other_user)

        # Create consent for original user
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Try to access consent of another user
        url = reverse(
            "marketplace-user-offering-consent-detail",
            kwargs={"uuid": consent.uuid},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_offering_consent_methods(self):
        """Test Offering model consent-related methods."""
        self.assertTrue(self.offering.has_terms_of_service())

        self.assertIsNone(self.offering.check_user_consent(self.user))

        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        retrieved_consent = self.offering.check_user_consent(self.user)
        self.assertEqual(retrieved_consent, consent)

        # Test get_active_consents
        active_consents = self.offering.get_active_consents()
        self.assertEqual(len(active_consents), 1)
        self.assertEqual(active_consents[0], consent)

    def test_user_offering_consent_model_methods(self):
        """Test UserOfferingConsent model methods."""
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Test is_revoked property
        self.assertFalse(consent.is_revoked)

        # Test revoke method
        consent.revoke()
        self.assertIsNotNone(consent.revocation_date)
        self.assertTrue(consent.is_revoked)

    def test_consent_with_anonymous_user(self):
        """Test consent functionality with anonymous user."""
        response = self.client.post(self.consent_list_url, self.consent_data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_consent_with_invalid_data(self):
        """Test consent creation with invalid data."""
        self.client.force_authenticate(user=self.user)

        # Test with missing required fields
        response = self.client.post(self.consent_list_url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Test with non-existent offering
        data = {
            "offering": uuid.uuid4(),
        }
        response = self.client.post(self.consent_list_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_consent_with_revoked_consent(self):
        """Test that revoked consent is not considered active."""
        self.client.force_authenticate(user=self.user)

        # Create and revoke consent
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()

        # Check consent status via filtering - only active consents
        response = self.client.get(
            f"{self.consent_list_url}?offering_uuid={self.offering.uuid}&user_uuid={self.user.uuid}&has_consent=true"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)  # No active consents found

    def test_consent_across_different_projects(self):
        """Test that consent is user-offering level, not project-specific."""
        self.client.force_authenticate(user=self.user)

        # Create another project
        other_project = ProjectFactory(customer=self.customer)
        other_project.add_user(self.user, role=ProjectRole.MANAGER)

        # Create consent for the offering
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Check consent status - should be the same for both projects since consent is user-offering level
        response = self.client.get(
            f"{self.consent_list_url}?offering_uuid={self.offering.uuid}&user_uuid={self.user.uuid}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["has_consent"])

        response = self.client.get(
            f"{self.consent_list_url}?offering_uuid={self.offering.uuid}&user_uuid={self.user.uuid}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["has_consent"])

    def test_consent_version_tracking(self):
        """Test that consent version is properly tracked."""
        self.client.force_authenticate(user=self.user)

        # Create consent with specific version
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="2.0",
        )

        # Check that version is tracked
        response = self.client.get(
            f"{self.consent_list_url}?offering_uuid={self.offering.uuid}&user_uuid={self.user.uuid}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["version"], "2.0")

    def test_has_consent_filter_true(self):
        """Test has_consent filter when value is True."""
        self.client.force_authenticate(user=self.user)

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        response = self.client.get(f"{self.consent_list_url}?has_consent=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["has_consent"])

    def test_has_consent_filter_false(self):
        """Test has_consent filter when value is False."""
        self.client.force_authenticate(user=self.user)

        # Create revoked consent
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()

        # Test filter for inactive consents
        response = self.client.get(f"{self.consent_list_url}?has_consent=false")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertFalse(response.data[0]["has_consent"])

    def test_requires_reconsent_filter_true(self):
        """Test requires_reconsent filter when value is True."""
        self.client.force_authenticate(user=self.user)

        # Deactivate the existing ToS config
        deactivate_tos_config(self.tos_config)

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Updated Terms of Service",
            version="2.0",
            requires_reconsent=True,
            is_active=True,
        )

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        response = self.client.get(f"{self.consent_list_url}?requires_reconsent=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["requires_reconsent"])

    def test_requires_reconsent_filter_false(self):
        """Test requires_reconsent filter when value is False."""
        self.client.force_authenticate(user=self.user)

        # Deactivate the existing ToS config
        deactivate_tos_config(self.tos_config)

        # Create ToS that doesn't require reconsent
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Updated Terms of Service",
            version="2.0",
            requires_reconsent=False,
        )

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="2.0",
        )

        response = self.client.get(f"{self.consent_list_url}?requires_reconsent=false")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertFalse(response.data[0]["requires_reconsent"])

    def test_offering_user_creation_with_consent(self):
        """Test that OfferingUser is created when user has consented to ToS."""
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Trigger offering user creation by adding user to project
        self.project.add_user(self.user, role=ProjectRole.MANAGER)

        # Check that OfferingUser was created
        offering_user = models.OfferingUser.objects.filter(
            user=self.user,
            offering=self.offering,
        ).first()
        self.assertIsNotNone(offering_user)
        self.assertEqual(offering_user.username, self.user.username)

    def test_offering_user_creation_without_consent(self):
        """Test that OfferingUser is created when user has not consented to ToS."""
        # Don't create consent - user has not agreed to ToS

        # Trigger offering user creation by adding user to project
        self.project.add_user(self.user, role=ProjectRole.MANAGER)

        offering_user = models.OfferingUser.objects.filter(
            user=self.user,
            offering=self.offering,
        ).first()

        self.assertIsNotNone(offering_user)

    def test_offering_user_creation_for_new_resource_with_consent(self):
        """Test that OfferingUser is created for new resource when user has consented."""
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        new_resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        new_resource.state = ResourceStates.OK
        new_resource.save()

        resource_creation_succeeded(new_resource)

        # Check that OfferingUser was created
        offering_user = models.OfferingUser.objects.filter(
            user=self.user,
            offering=self.offering,
        ).exists()

        self.assertTrue(offering_user)

    def test_offering_user_creation_for_new_resource_without_consent(self):
        """Test that OfferingUser is created for new resource when user has not consented."""
        # Don't create consent - user has not agreed to ToS

        # Create a new resource to trigger offering user creation
        new_resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        new_resource.state = ResourceStates.OK
        new_resource.save()

        # Trigger the resource creation succeeded callback
        resource_creation_succeeded(new_resource)

        offering_user = models.OfferingUser.objects.filter(
            user=self.user,
            offering=self.offering,
        ).first()

        self.assertIsNotNone(offering_user)

    def test_offering_user_creation_with_revoked_consent(self):
        """Test that OfferingUser is created when consent has been revoked."""
        # Create and then revoke consent
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()

        # Trigger offering user creation by adding user to project
        self.project.add_user(self.user, role=ProjectRole.MANAGER)

        offering_user = models.OfferingUser.objects.filter(
            user=self.user,
            offering=self.offering,
        ).first()

        self.assertIsNotNone(offering_user)

    def test_offering_user_creation_multiple_users_with_mixed_consent(self):
        """Test offering user creation with multiple users, some with consent, some without."""
        # Create another user
        other_user = UserFactory()
        self.project.add_user(other_user, role=ProjectRole.MEMBER)

        # Create consent only for the first user
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Create a new resource to trigger offering user creation
        new_resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        new_resource.state = ResourceStates.OK
        new_resource.save()

        # Trigger the resource creation succeeded callback
        resource_creation_succeeded(new_resource)

        offering_user_1 = models.OfferingUser.objects.filter(
            user=self.user,
            offering=self.offering,
        ).first()
        offering_user_2 = models.OfferingUser.objects.filter(
            user=other_user,
            offering=self.offering,
        ).first()

        self.assertIsNotNone(offering_user_1)
        self.assertIsNotNone(offering_user_2)

    def test_offering_user_creation_project_specific_consent(self):
        """Test that consent is user-offering level for offering user creation."""
        other_project = ProjectFactory(customer=self.customer)
        other_project.add_user(self.user, role=ProjectRole.MANAGER)

        # Create consent for the offering (not project-specific)
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Create resources in both projects
        resource_1 = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        resource_1.state = ResourceStates.OK
        resource_1.save()
        resource_creation_succeeded(resource_1)

        resource_2 = ResourceFactory(
            project=other_project,
            offering=self.offering,
            plan=self.plan,
        )
        resource_2.state = ResourceStates.OK
        resource_2.save()
        resource_creation_succeeded(resource_2)

        # Check that OfferingUser was created for the user in both projects (same user-offering)
        offering_user = models.OfferingUser.objects.filter(
            user=self.user,
            offering=self.offering,
        ).first()

        self.assertIsNotNone(offering_user)

    def test_user_without_consent_hidden_from_service_provider(self):
        """Test that users without consent are hidden from service provider views."""
        # Don't create consent - user has not agreed to ToS

        # Service provider should NOT see the user (no consent)
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertNotIn(str(self.user.uuid), user_uuids)

    def test_granting_consent_makes_user_visible_to_service_provider(self):
        """Test that granting consent makes user visible to service provider views."""
        # Start without consent
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        # User should not be visible initially
        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertNotIn(str(self.user.uuid), user_uuids)

        # Grant consent
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # User should now be visible
        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(self.user.uuid), user_uuids)

    def test_consent_revocation_hides_user_from_service_provider(self):
        """Test that revoking consent hides user from service provider views."""
        # Create consent first
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Service provider should see the user (with consent)
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(self.user.uuid), user_uuids)

        consent.revoke()

        # Service provider should no longer see the user (without consent)
        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertNotIn(str(self.user.uuid), user_uuids)

    def test_staff_and_support_can_see_all_users_regardless_of_consent(self):
        """Test that staff and support users can see all offering users regardless of consent."""
        # Don't create consent - user has not agreed to ToS

        # Staff user should see the user (despite no consent)
        staff_user = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(self.user.uuid), user_uuids)

        # Support user should also see the user (despite no consent)
        support_user = UserFactory(is_support=True)
        self.client.force_authenticate(user=support_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(self.user.uuid), user_uuids)

    def test_user_can_see_own_offering_user_record_without_consent(self):
        """Test that users can see their own OfferingUser record even without consent."""

        # User should be able to see their own OfferingUser record
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(self.user.uuid), user_uuids)

    def test_mixed_consent_visibility(self):
        """Test that SP sees only consented users when multiple users have mixed consent status."""
        # Create another user without consent
        user_without_consent = UserFactory()
        self.project.add_user(user_without_consent, role=ProjectRole.MEMBER)

        # Create third user with consent
        user_with_consent = UserFactory()
        self.project.add_user(user_with_consent, role=ProjectRole.MEMBER)
        models.UserOfferingConsent.objects.create(
            user=user_with_consent,
            offering=self.offering,
            version="1.0",
        )

        # Grant consent to main test user too
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Service provider should only see users with consent
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]

        # Should see users with consent
        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertIn(str(user_with_consent.uuid), user_uuids)

        # Should NOT see user without consent
        self.assertNotIn(str(user_without_consent.uuid), user_uuids)

    def test_inactive_consent_hides_user(self):
        """Test that users with revoked consent are hidden from service providers."""
        # Create and revoke consent
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()  # Revoked consent

        # Service provider should NOT see the user (revoked consent)
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertNotIn(str(self.user.uuid), user_uuids)

    def test_consent_revocation_no_offering_user(self):
        """Test that revoking consent handles case where no OfferingUser exists."""
        new_user = UserFactory()
        # Add user to project and delete offering user
        self.project.add_user(new_user, role=ProjectRole.MEMBER)
        models.OfferingUser.objects.filter(
            user=new_user,
            offering=self.offering,
        ).delete()

        # Create consent but no offering user
        consent = models.UserOfferingConsent.objects.create(
            user=new_user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()

        offering_user = models.OfferingUser.objects.filter(
            user=new_user,
            offering=self.offering,
        ).exists()
        self.assertFalse(offering_user)

    def test_order_creation_requires_consent(self):
        """Test that order creation requires at least one user to consent to ToS."""
        self.client.force_authenticate(user=self.user)

        # Try to create an order with consent
        order_data = {
            "offering": OfferingFactory.get_public_url(self.offering),
            "project": ProjectFactory.get_url(self.project),
            "plan": PlanFactory.get_public_url(self.plan),
            "attributes": {"name": "Test Resource"},
            "accepting_terms_of_service": True,
        }
        url = OrderFactory.get_list_url()
        response = self.client.post(url, order_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the order was created
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.offering, self.offering)
        self.assertEqual(order.project, self.project)
        self.assertEqual(order_data["attributes"]["name"], order.attributes["name"])

    def test_order_creation_without_tos_does_not_require_consent(self):
        """Test that order creation for offerings without ToS does not require consent."""
        # Create an offering without ToS
        offering_without_tos = OfferingFactory(
            category=self.category,
            customer=self.customer,
            type="Marketplace.Basic",
        )
        plan_without_tos = PlanFactory(offering=offering_without_tos)

        self.client.force_authenticate(user=self.user)

        # should work since no ToS
        order_data = {
            "offering": OfferingFactory.get_public_url(offering_without_tos),
            "project": ProjectFactory.get_url(self.project),
            "plan": PlanFactory.get_public_url(plan_without_tos),
            "attributes": {"name": "Test Resource"},
            "accepting_terms_of_service": True,
        }

        response = self.client.post(OrderFactory.get_list_url(), order_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.offering, offering_without_tos)
        self.assertEqual(order.project, self.project)

    def test_order_creation_with_multiple_users_consent(self):
        """Test that order creation works when multiple users have consented."""
        # Create another user and add to project
        other_user = UserFactory()
        self.project.add_user(other_user, role=ProjectRole.MEMBER)

        # Grant consent to the other user (not the current user)
        models.UserOfferingConsent.objects.create(
            user=other_user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        # Create order should work since at least one user has agreed to ToS
        order_data = {
            "offering": OfferingFactory.get_public_url(self.offering),
            "project": ProjectFactory.get_url(self.project),
            "plan": PlanFactory.get_public_url(self.plan),
            "attributes": {"name": "Test Resource"},
            "accepting_terms_of_service": True,
        }

        response = self.client.post(OrderFactory.get_list_url(), order_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.offering, self.offering)
        self.assertEqual(order.project, self.project)

    def test_order_creation_with_revoked_consent_succeeds_when_reconsenting(self):
        """Test that order creation succeeds when reconsenting to the ToS."""
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Revoke consent
        consent.revoke()

        self.client.force_authenticate(user=self.user)

        # Try to create an order - should fail since consent was revoked
        order_data = {
            "offering": OfferingFactory.get_public_url(self.offering),
            "project": ProjectFactory.get_url(self.project),
            "plan": PlanFactory.get_public_url(self.plan),
            "attributes": {"name": "Test Resource"},
            "accepting_terms_of_service": True,  # This reconsents to the ToS for the user
        }

        response = self.client.post(OrderFactory.get_list_url(), order_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_order_creation_succeeds_when_admin_user(self):
        """Test that order creation succeeds when admin user."""
        self.client.force_authenticate(user=UserFactory(is_staff=True))
        order_data = {
            "offering": OfferingFactory.get_public_url(self.offering),
            "project": ProjectFactory.get_url(self.project),
            "plan": PlanFactory.get_public_url(self.plan),
            "attributes": {"name": "Test Resource"},
        }
        response = self.client.post(OrderFactory.get_list_url(), order_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_conditional_consent_filtering_when_enabled(self):
        """Test that consent filtering is applied when ENFORCE_USER_CONSENT_FOR_OFFERINGS is True."""
        user_without_consent = UserFactory()
        self.project.add_user(user_without_consent, role=ProjectRole.MEMBER)

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]

        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertNotIn(str(user_without_consent.uuid), user_uuids)

    def test_conditional_consent_filtering_when_disabled(self):
        """Test that consent filtering is NOT applied when ENFORCE_USER_CONSENT_FOR_OFFERINGS is False."""
        user_without_consent = UserFactory()
        self.project.add_user(user_without_consent, role=ProjectRole.MEMBER)

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Service provider should see all users when enforcement is disabled
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        with override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False):
            response = self.client.get("/api/marketplace-offering-users/")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            user_uuids = [ou["user_uuid"] for ou in response.data]

            self.assertIn(str(self.user.uuid), user_uuids)
            self.assertIn(str(user_without_consent.uuid), user_uuids)

    def test_conditional_consent_filtering_single_offering_with_tos(self):
        """Test consent filtering for a single offering that has ToS requirements."""
        user_without_consent = UserFactory()
        self.project.add_user(user_without_consent, role=ProjectRole.MEMBER)

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]

        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertNotIn(str(user_without_consent.uuid), user_uuids)

        with override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False):
            response = self.client.get("/api/marketplace-offering-users/")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            user_uuids = [ou["user_uuid"] for ou in response.data]

            self.assertIn(str(self.user.uuid), user_uuids)
            self.assertIn(str(user_without_consent.uuid), user_uuids)

    def test_conditional_consent_filtering_offering_users_endpoint(self):
        """Test that the OfferingUsers endpoint respects the conditional consent filtering."""
        user_without_consent = UserFactory()
        self.project.add_user(user_without_consent, role=ProjectRole.MEMBER)

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]

        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertNotIn(str(user_without_consent.uuid), user_uuids)

        with override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False):
            response = self.client.get("/api/marketplace-offering-users/")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            user_uuids = [ou["user_uuid"] for ou in response.data]

            self.assertIn(str(self.user.uuid), user_uuids)
            self.assertIn(str(user_without_consent.uuid), user_uuids)

    def test_conditional_consent_filtering_customer_users_endpoint(self):
        """Test that the customer users endpoint respects the conditional consent filtering."""
        user_without_consent = UserFactory()
        self.project.add_user(user_without_consent, role=ProjectRole.MEMBER)
        self.customer.add_user(self.user, CustomerRole.OWNER)

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(
            f"/api/marketplace-provider-offerings/{self.offering.uuid}/list_customer_users/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [user["uuid"] for user in response.data]

        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertNotIn(str(user_without_consent.uuid), user_uuids)

        with override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False):
            response = self.client.get(
                f"/api/marketplace-provider-offerings/{self.offering.uuid}/list_customer_users/"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            user_uuids = [user["uuid"] for user in response.data]

            # Should see both users when enforcement is disabled
            self.assertIn(str(self.user.uuid), user_uuids)
            self.assertIn(str(user_without_consent.uuid), user_uuids)

    def test_user_visibility_only_for_non_tos_offering_when_no_consent(self):
        """Test that users without consent only appear for non-ToS offerings."""
        offering_no_tos = OfferingFactory(
            customer=self.customer,
            type="Marketplace.Basic",
            category=self.category,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
            },
        )
        plan_no_tos = PlanFactory(offering=offering_no_tos)

        user_no_consent = UserFactory()

        project2 = ProjectFactory(customer=self.customer)

        resource_tos = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        resource_tos.state = ResourceStates.OK
        resource_tos.save()

        resource_no_tos = ResourceFactory(
            project=project2,
            offering=offering_no_tos,
            plan=plan_no_tos,
        )
        resource_no_tos.state = ResourceStates.OK
        resource_no_tos.save()

        self.project.add_user(user_no_consent, role=ProjectRole.MANAGER)
        project2.add_user(user_no_consent, role=ProjectRole.MANAGER)

        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_uuids = [ou["user_uuid"] for ou in response.data]

        self.assertIn(str(user_no_consent.uuid), user_uuids)

        # Verify user only appears for the non-ToS offering
        user_records = [
            ou for ou in response.data if ou["user_uuid"] == str(user_no_consent.uuid)
        ]
        self.assertEqual(len(user_records), 1)

        offering_uuids_for_user = [ou["offering_uuid"] for ou in user_records]
        self.assertIn(str(offering_no_tos.uuid), offering_uuids_for_user)
        self.assertNotIn(str(self.offering.uuid), offering_uuids_for_user)

        response = self.client.get(
            f"/api/marketplace-provider-offerings/{self.offering.uuid}/list_customer_users/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer_user_uuids = [user["uuid"] for user in response.data]

        self.assertNotIn(str(user_no_consent.uuid), customer_user_uuids)

        response = self.client.get(
            f"/api/marketplace-provider-offerings/{offering_no_tos.uuid}/list_customer_users/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        no_tos_customer_user_uuids = [user["uuid"] for user in response.data]

        # Should see user for non-ToS offering
        self.assertIn(str(user_no_consent.uuid), no_tos_customer_user_uuids)

    def test_conditional_consent_filtering_mixed_offerings(self):
        """Test consent filtering with mixed offerings (some with ToS, some without)."""
        offering_without_tos = OfferingFactory(
            category=self.category,
            customer=self.customer,
            type="Marketplace.Basic",
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
            },
        )

        plan_without_tos = PlanFactory(offering=offering_without_tos)
        resource_without_tos = ResourceFactory(
            project=self.project,
            offering=offering_without_tos,
            plan=plan_without_tos,
        )
        resource_without_tos.state = ResourceStates.OK
        resource_without_tos.save()

        # Create another user without consent for the main offering
        user_without_consent = UserFactory()

        # Create a THIRD user who also has no consent for main offering
        user_no_consent_main = UserFactory()

        self.project.add_user(user_without_consent, role=ProjectRole.MEMBER)
        self.project.add_user(user_no_consent_main, role=ProjectRole.MEMBER)

        # Create consent only for the main user for the main offering
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Service provider should see all users for offerings without ToS
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)
        response = self.client.get("/api/marketplace-offering-users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]

        # Should see user with consent for main offering and user without consent for non-ToS offering
        # 5 offering users, but only 3 users should be visible
        self.assertEqual(len(user_uuids), 3)
        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertIn(str(user_without_consent.uuid), user_uuids)

        self.assertIn(str(user_no_consent_main.uuid), user_uuids)

        user_no_consent_main_records = [
            ou
            for ou in response.data
            if ou["user_uuid"] == str(user_no_consent_main.uuid)
        ]

        self.assertEqual(len(user_no_consent_main_records), 1)
        self.assertEqual(
            user_no_consent_main_records[0]["offering_uuid"],
            str(offering_without_tos.uuid),
        )

        with override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False):
            response = self.client.get("/api/marketplace-offering-users/")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            user_uuids = [ou["user_uuid"] for ou in response.data]

            # Should see ALL users when enforcement is disabled
            self.assertIn(str(self.user.uuid), user_uuids)
            self.assertIn(str(user_without_consent.uuid), user_uuids)
            self.assertIn(str(user_no_consent_main.uuid), user_uuids)
            self.assertEqual(len(user_uuids), 5)

    def test_offering_user_serializer_consent_fields_with_consent(self):
        """Test that OfferingUserSerializer includes consent fields when user has consent."""
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        offering_user, created = models.OfferingUser.objects.get_or_create(
            user=self.user,
            offering=self.offering,
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(
            f"/api/marketplace-offering-users/{offering_user.uuid}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("has_consent", response.data)
        self.assertIn("requires_reconsent", response.data)
        self.assertTrue(response.data["has_consent"])
        self.assertFalse(response.data["requires_reconsent"])

    def test_offering_user_serializer_consent_fields_without_consent(self):
        """Test that OfferingUserSerializer includes consent fields when user has no consent."""
        offering_user, created = models.OfferingUser.objects.get_or_create(
            user=self.user,
            offering=self.offering,
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(
            f"/api/marketplace-offering-users/{offering_user.uuid}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("has_consent", response.data)
        self.assertIn("requires_reconsent", response.data)
        self.assertFalse(response.data["has_consent"])
        self.assertFalse(response.data["requires_reconsent"])

    def test_offering_user_filter_has_consent_true(self):
        """Test filtering offering users by has_consent=true with specific user_uuid."""
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # This user should not be visible because they don't have consent and the view filters them out
        other_user = UserFactory()
        self.project.add_user(other_user, role=ProjectRole.MEMBER)
        other_offering_user, created = models.OfferingUser.objects.get_or_create(
            user=other_user,
            offering=self.offering,
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(
            f"/api/marketplace-offering-users/?user_uuid={self.user.uuid}&has_consent=true"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertEqual(len(user_uuids), 1)

        response = self.client.get(
            f"/api/marketplace-offering-users/?user_uuid={other_user.uuid}&has_consent=true"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertEqual(len(user_uuids), 0)

    def test_offering_user_filter_has_consent_false(self):
        """Test filtering offering users by has_consent=false with specific user_uuid."""
        other_user = UserFactory()
        self.project.add_user(other_user, role=ProjectRole.MEMBER)

        self.client.force_authenticate(user=self.user)

        response = self.client.get(
            f"/api/marketplace-offering-users/?user_uuid={self.user.uuid}&has_consent=false"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(self.user.uuid), user_uuids)
        self.assertEqual(len(user_uuids), 1)

        response = self.client.get(
            f"/api/marketplace-offering-users/?user_uuid={other_user.uuid}&has_consent=false"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertEqual(len(user_uuids), 0)

        # Add consent for other user
        models.UserOfferingConsent.objects.create(
            user=other_user,
            offering=self.offering,
            version="1.0",
        )
        response = self.client.get(
            f"/api/marketplace-offering-users/?user_uuid={other_user.uuid}&has_consent=true"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user_uuids = [ou["user_uuid"] for ou in response.data]
        self.assertIn(str(other_user.uuid), user_uuids)


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class ProviderOfferingToSManagementViewsetTest(APITransactionTestCase):
    """Test cases for ProviderOfferingToSManagementViewset."""

    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.user = UserFactory()
        self.customer = CustomerFactory()
        self.project = ProjectFactory(customer=self.customer)
        self.project.add_user(self.user, role=ProjectRole.MANAGER)

        self.service_provider = ServiceProviderFactory(customer=self.customer)

        self.offering = OfferingFactory(
            customer=self.customer,
            type="Marketplace.Basic",
            state=OfferingStates.ACTIVE,
            plugin_options={
                "service_provider_can_create_offering_user": True,
            },
        )
        self.tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Initial terms of service",
            terms_of_service_link="https://example.com/tos",
            version="1.0",
            is_active=True,
        )
        self.plan = PlanFactory(offering=self.offering)
        self.resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.list_url = "/api/marketplace-offering-terms-of-service/"
        self.detail_url = (
            f"/api/marketplace-offering-terms-of-service/{self.tos_config.uuid}/"
        )

    def test_list_terms_of_service_configs(self):
        """Test listing ToS configurations."""
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.tos_config.uuid))
        self.assertEqual(
            response.data[0]["terms_of_service"], "Initial terms of service"
        )
        self.assertEqual(response.data[0]["version"], "1.0")

    def test_create_terms_of_service_config(self):
        """Test creating a new ToS configuration."""
        self.client.force_authenticate(user=self.user)

        # Deactivate the existing ToS config
        deactivate_tos_config(self.tos_config)

        offering_url = OfferingFactory.get_url(self.offering)
        data = {
            "offering": offering_url,
            "terms_of_service": "New terms of service",
            "terms_of_service_link": "https://example.com/new-tos",
            "version": "2.0",
            "requires_reconsent": True,
        }

        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the ToS config was created
        tos_config = models.OfferingTermsOfService.objects.get(version="2.0")
        self.assertEqual(tos_config.terms_of_service, "New terms of service")
        self.assertTrue(tos_config.requires_reconsent)

    def test_create_terms_of_service_config_with_existing_active_tos(self):
        """Test creating a new ToS configuration with an existing active ToS config."""
        self.client.force_authenticate(user=self.user)
        data = {
            "offering": OfferingFactory.get_url(self.offering),
            "terms_of_service": "New terms of service",
            "version": "2.0",
            "requires_reconsent": True,
            "is_active": True,
        }

        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "An active Terms of Service configuration already exists for this offering",
            response.data["non_field_errors"][0],
        )

    def test_create_inactive_terms_of_service_config_with_existing_active_tos(self):
        """Test creating an inactive ToS configuration when an active one exists."""
        self.client.force_authenticate(user=self.user)
        data = {
            "offering": OfferingFactory.get_url(self.offering),
            "terms_of_service": "Inactive terms of service",
            "version": "2.0",
            "requires_reconsent": True,
            "is_active": False,  # Inactive
        }

        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the ToS config was created
        tos_config = models.OfferingTermsOfService.objects.get(version="2.0")
        self.assertEqual(tos_config.terms_of_service, "Inactive terms of service")
        self.assertFalse(tos_config.is_active)
        self.assertTrue(tos_config.requires_reconsent)

    def test_service_provider_users_endpoint_filter(self):
        """Test that ServiceProviderUsersViewSet works after fixing the resource relationship."""
        service_provider_user = UserFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)
        self.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        user1 = UserFactory()
        user2 = UserFactory()
        user3 = UserFactory()

        self.project.add_user(user1, role=ProjectRole.MANAGER)
        self.project.add_user(user2, role=ProjectRole.ADMIN)
        self.project.add_user(user3, role=ProjectRole.MEMBER)

        # Create user consents for ToS-required offerings
        models.UserOfferingConsent.objects.create(
            user=user1,
            offering=self.offering,
            version="1.0",
        )
        models.UserOfferingConsent.objects.create(
            user=user2,
            offering=self.offering,
            version="1.0",
        )

        url = f"/api/marketplace-service-providers/{self.service_provider.uuid}/users/"

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data

        # Should only return users who have ToS consent
        returned_user_ids = [user["uuid"] for user in data]
        self.assertIn(str(user1.uuid), returned_user_ids)
        self.assertIn(str(user2.uuid), returned_user_ids)

    def test_service_provider_users_endpoint_without_tos_offering(self):
        """Test that users are visible when using offerings without ToS requirements."""
        service_provider_user = UserFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)
        self.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        # Create an offering WITHOUT terms of service
        offering_without_tos = OfferingFactory(
            customer=self.customer,
            type="Marketplace.Basic",
            state=OfferingStates.ACTIVE,
            plugin_options={
                "service_provider_can_create_offering_user": True,
            },
        )

        user_without_tos = UserFactory()
        user_without_tos2 = UserFactory()

        self.project.add_user(user_without_tos, role=ProjectRole.MANAGER)
        self.project.add_user(user_without_tos2, role=ProjectRole.ADMIN)

        models.OfferingUser.objects.create(
            user=user_without_tos,
            offering=offering_without_tos,
        )
        models.OfferingUser.objects.create(
            user=user_without_tos2,
            offering=offering_without_tos,
        )

        user_no_resource = UserFactory()
        self.project.add_user(user_no_resource, role=ProjectRole.MEMBER)

        url = f"/api/marketplace-service-providers/{self.service_provider.uuid}/users/"

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        returned_user_ids = [user["uuid"] for user in data]
        self.assertIn(str(user_without_tos.uuid), returned_user_ids)
        self.assertIn(str(user_without_tos2.uuid), returned_user_ids)

        self.assertNotIn(str(user_no_resource.uuid), returned_user_ids)

    def test_service_provider_users_endpoint_works_when_consent_disabled(self):
        """Test that the endpoint works when ENFORCE_USER_CONSENT_FOR_OFFERINGS is False."""
        service_provider_user = UserFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)
        self.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        url = f"/api/marketplace-service-providers/{self.service_provider.uuid}/users/"

        with override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), 1)

    def test_marketplace_provider_customer_serializer_with_tos_consent(self):
        """Test that MarketplaceProviderCustomerSerializer.get_users_qs works with ToS consent."""
        service_provider_user = UserFactory()
        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS
        )
        self.customer.add_user(service_provider_user, CustomerRole.OWNER)
        self.client.force_authenticate(user=service_provider_user)

        # Create users with ToS consent
        user_with_consent = UserFactory()
        user_without_consent = UserFactory()

        # Add users to project
        self.project.add_user(user_with_consent, role=ProjectRole.MANAGER)
        self.project.add_user(user_without_consent, role=ProjectRole.ADMIN)

        # Create consent for one user
        models.UserOfferingConsent.objects.create(
            user=user_with_consent,
            offering=self.offering,
            version="1.0",
        )

        url = f"/api/marketplace-service-providers/{self.service_provider.uuid}/customers/"

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return the customer
        self.assertEqual(len(response.data), 1)
        customer_data = response.data[0]

        # Check that users_count reflects only users with consent
        self.assertEqual(customer_data["users_count"], 1)

        # Check that users field contains only the user with consent
        users_data = customer_data["users"]
        self.assertEqual(len(users_data), 1)
        self.assertEqual(users_data[0]["uuid"], str(user_with_consent.uuid))

    def test_update_terms_of_service_config(self):
        """Test updating an existing ToS configuration."""
        self.client.force_authenticate(user=self.user)

        data = {
            "terms_of_service": "Updated terms of service",
            "version": "1.1",
            "requires_reconsent": True,
        }

        response = self.client.patch(self.detail_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the ToS config was updated
        self.tos_config.refresh_from_db()
        self.assertEqual(self.tos_config.terms_of_service, "Updated terms of service")
        self.assertEqual(self.tos_config.version, "1.1")
        self.assertTrue(self.tos_config.requires_reconsent)

    def test_update_terms_of_service_config_duplicate_active_validation(self):
        """Test that updating ToS config to active fails when another active exists."""
        self.client.force_authenticate(user=self.user)

        other_tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Other terms of service",
            terms_of_service_link="https://example.com/other-tos",
            version="2.0",
            is_active=False,
        )

        data = {"is_active": True}
        other_detail_url = (
            f"/api/marketplace-offering-terms-of-service/{other_tos_config.uuid}/"
        )

        response = self.client.put(other_detail_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("non_field_errors", response.data)
        self.assertIn("already exists", response.data["non_field_errors"][0])

    def test_delete_terms_of_service_config(self):
        """Test deleting a ToS configuration."""
        self.client.force_authenticate(user=self.user)

        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify the ToS config was deleted
        self.assertFalse(
            models.OfferingTermsOfService.objects.filter(
                uuid=self.tos_config.uuid
            ).exists()
        )

    def test_create_terms_of_service_config_requires_permission(self):
        """Test that creating ToS config requires proper permissions."""
        user_without_permission = UserFactory()
        self.project.add_user(user_without_permission, role=ProjectRole.MEMBER)
        self.client.force_authenticate(user=user_without_permission)
        offering_url = OfferingFactory.get_url(self.offering)
        data = {
            "offering": offering_url,
            "terms_of_service": "New terms",
            "version": "2.0",
        }

        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filter_terms_of_service_by_offering(self):
        """Test filtering ToS configurations by offering."""
        self.client.force_authenticate(user=self.user)

        other_offering = OfferingFactory(customer=self.customer)
        models.OfferingTermsOfService.objects.create(
            offering=other_offering,
            terms_of_service="Other terms",
            version="1.0",
        )

        response = self.client.get(
            f"{self.list_url}?offering_uuid={self.offering.uuid}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.tos_config.uuid))

    def test_filter_terms_of_service_by_active_status(self):
        """Test filtering ToS configurations by active status."""
        self.client.force_authenticate(user=self.user)

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Inactive terms",
            version="2.0",
            is_active=False,
        )

        response = self.client.get(f"{self.list_url}?is_active=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.tos_config.uuid))

    def test_terms_of_service_version_tracking(self):
        """Test that ToS version tracking works correctly."""
        self.client.force_authenticate(user=self.user)

        # Deactivate the existing ToS config
        deactivate_tos_config(self.tos_config)

        # Create multiple versions
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Version 2 terms",
            version="2.0",
        )

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Version 3 terms",
            version="3.0",
        )

        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

        # Check that versions are ordered by creation date (newest first)
        versions = [item["version"] for item in response.data]
        self.assertEqual(versions, ["3.0", "2.0", "1.0"])

    def test_terms_of_service_serializer_user_consent_fields_with_consent(self):
        """Test that user_consent and has_user_consent fields work when user has consented."""
        self.client.force_authenticate(user=self.user)

        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIsNotNone(response.data["user_consent"])
        self.assertEqual(str(response.data["user_consent"]["uuid"]), str(consent.uuid))
        self.assertEqual(response.data["user_consent"]["version"], "1.0")
        self.assertIsNotNone(response.data["user_consent"]["agreement_date"])
        self.assertFalse(response.data["user_consent"]["is_revoked"])

        self.assertTrue(response.data["has_user_consent"])

    def test_terms_of_service_serializer_user_consent_fields_without_consent(self):
        """Test that user_consent and has_user_consent fields work when user has not consented."""
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIsNone(response.data["user_consent"])

        self.assertFalse(response.data["has_user_consent"])

    def test_terms_of_service_serializer_user_consent_fields_with_revoked_consent(self):
        """Test that user_consent and has_user_consent fields work when consent is revoked."""
        self.client.force_authenticate(user=self.user)

        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIsNone(response.data["user_consent"])

        self.assertFalse(response.data["has_user_consent"])

    def test_terms_of_service_serializer_user_consent_fields_anonymous_user(self):
        """Test that anonymous users cannot access the endpoint (requires authentication)."""
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_terms_of_service_serializer_user_consent_fields_different_user(self):
        """Test that user_consent fields show correct data for different users."""
        other_user = UserFactory()
        other_consent = models.UserOfferingConsent.objects.create(
            user=other_user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIsNone(response.data["user_consent"])
        self.assertFalse(response.data["has_user_consent"])

        self.client.force_authenticate(user=other_user)

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIsNotNone(response.data["user_consent"])
        self.assertEqual(
            str(response.data["user_consent"]["uuid"]), str(other_consent.uuid)
        )
        self.assertTrue(response.data["has_user_consent"])

    def test_create_consent_after_revocation(self):
        """Test that creating consent after revocation works correctly."""
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()

        # Now try to create consent again
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/marketplace-user-offering-consents/",
            {"offering": str(self.offering.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        updated_consent = models.UserOfferingConsent.objects.get(
            user=self.user, offering=self.offering
        )
        self.assertEqual(updated_consent.uuid, consent.uuid)
        self.assertIsNone(updated_consent.revocation_date)
        self.assertEqual(updated_consent.version, "1.0")


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class ResourceToSConsentPermissionTest(APITransactionTestCase):
    """Test cases for resource access control based on ToS consent."""

    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_RESOURCES)

        self.user = UserFactory()
        self.customer = CustomerFactory()
        self.project = ProjectFactory(customer=self.customer)

        self.service_provider = ServiceProviderFactory(customer=self.customer)

        self.offering = OfferingFactory(
            customer=self.customer,
            type="Marketplace.Basic",
            state=OfferingStates.ACTIVE,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
            },
        )
        self.tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Initial terms of service",
            terms_of_service_link="https://example.com/tos",
            version="1.0",
            is_active=True,
        )
        self.plan = PlanFactory(offering=self.offering)
        self.resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.project.add_user(self.user, role=ProjectRole.MANAGER)

        self.resource_url = ResourceFactory.get_url(self.resource)

    def test_resource_access_with_consent_allowed(self):
        """Test that resource access is allowed when user has consented to ToS."""
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resource_access_staff_bypass(self):
        """Test that staff users can access resources without ToS consent."""
        staff_user = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        response = self.client.get(self.resource_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resource_access_support_bypass(self):
        """Test that support users can access resources without ToS consent."""
        support_user = UserFactory(is_support=True)
        self.client.force_authenticate(user=support_user)

        response = self.client.get(self.resource_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resource_access_no_tos_requirements(self):
        """Test that resource access is allowed when offering has no ToS requirements."""
        self.tos_config.delete()

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class ResourceConsentUIFieldsTest(APITransactionTestCase):
    """Test cases for user_requires_reconsent field in ResourceSerializer."""

    def setUp(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_RESOURCES)

        self.user = UserFactory()
        self.customer = CustomerFactory()
        self.project = ProjectFactory(customer=self.customer)

        self.service_provider = ServiceProviderFactory(customer=self.customer)

        self.offering = OfferingFactory(
            customer=self.customer,
            type="Marketplace.Basic",
            state=OfferingStates.ACTIVE,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
            },
        )

        # Create a component that supports limits
        self.offering_component = OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            billing_type=BillingTypes.LIMIT,
        )
        self.plan = PlanFactory(offering=self.offering)
        self.resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.project.add_user(self.user, role=ProjectRole.MANAGER)
        self.resource_list_url = ResourceFactory.get_list_url()

    def test_requires_reconsent_false_when_no_tos(self):
        """Test that user_requires_reconsent is False when offering has no ToS."""
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertFalse(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_false_for_staff_users(self):
        """Test that staff users never require reconsent."""
        staff_user = UserFactory(is_staff=True)
        self.project.add_user(staff_user, role=ProjectRole.MANAGER)

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
            requires_reconsent=True,
        )

        self.client.force_authenticate(user=staff_user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertFalse(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_false_for_support_users(self):
        """Test that support users never require reconsent."""
        support_user = UserFactory(is_support=True)
        self.project.add_user(support_user, role=ProjectRole.MANAGER)

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
            requires_reconsent=True,
        )

        self.client.force_authenticate(user=support_user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertFalse(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_true_when_no_consent_exists(self):
        """Test that user_requires_reconsent is True when user has no consent."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
            requires_reconsent=True,
            is_active=True,
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertTrue(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_false_when_tos_not_requires_reconsent(self):
        """Test that user_requires_reconsent is False when ToS doesn't require reconsent."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
            requires_reconsent=False,
        )

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertFalse(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_false_when_consent_version_matches(self):
        """Test that user_requires_reconsent is False when consent version matches ToS version."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="2.0",
            requires_reconsent=True,
        )

        # Create user consent with matching version
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="2.0",  # Same version as ToS
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertFalse(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_true_when_consent_version_outdated(self):
        """Test that user_requires_reconsent is True when consent version is outdated."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Updated Terms of Service",
            version="2.0",
            requires_reconsent=True,
            is_active=True,
        )

        # Create user consent with old version
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",  # Old version
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertTrue(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_false_when_no_active_tos(self):
        """Test that user_requires_reconsent is False when there's no active ToS."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
            is_active=False,
            requires_reconsent=True,
        )

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertFalse(resource_data["user_requires_reconsent"])

    def test_requires_reconsent_workflow_after_updating_consent(self):
        """Test the complete workflow: outdated consent -> re-consent -> no longer requires reconsent."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Updated Terms of Service",
            version="2.0",
            requires_reconsent=True,
            is_active=True,
        )

        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertTrue(resource_data["user_requires_reconsent"])

        consent.version = "2.0"
        consent.save()

        response = self.client.get(self.resource_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_data = next(
            (item for item in response.data if item["uuid"] == str(self.resource.uuid)),
            None,
        )
        self.assertIsNotNone(resource_data)
        self.assertFalse(resource_data["user_requires_reconsent"])

    def test_update_limits_requires_tos_consent(self):
        """Test that updating resource limits requires ToS consent."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
            is_active=True,
        )

        self.client.force_authenticate(user=self.user)

        update_limits_data = {"limits": self.resource.limits}
        update_limits_url = (
            f"/api/marketplace-resources/{self.resource.uuid}/update_limits/"
        )

        response = self.client.post(update_limits_url, update_limits_data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Terms of Service consent required", response.data["detail"])

    def test_update_limits_allowed_with_tos_consent(self):
        """Test that updating resource limits is allowed with ToS consent."""
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
        )

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(user=self.user)

        # Use the same limits that were set on the resource initially
        update_limits_data = {"limits": {"storage": 124}}
        update_limits_url = (
            f"/api/marketplace-resources/{self.resource.uuid}/update_limits/"
        )

        response = self.client.post(update_limits_url, update_limits_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class OfferingUsersViewSetPerformanceTest(APITransactionTestCase):
    """Test performance of OfferingUsersViewSet.get_queryset method."""

    def setUp(self):
        """Set up test data for performance testing."""
        # Create users
        self.user = UserFactory()
        self.service_provider_user = UserFactory()
        self.other_user = UserFactory()

        self.customer = CustomerFactory()
        self.project = ProjectFactory(customer=self.customer)

        self.project.add_user(self.user, role=ProjectRole.MANAGER)
        self.project.add_user(self.other_user, role=ProjectRole.MEMBER)

        self.service_provider = ServiceProviderFactory(customer=self.customer)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)
        self.customer.add_user(self.service_provider_user, CustomerRole.OWNER)

        self.offering = OfferingFactory(
            customer=self.customer,
            type="Marketplace.Basic",
            state=OfferingStates.ACTIVE,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
            },
        )

        self.tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Terms of Service",
            version="1.0",
            is_active=True,
        )

        self.plan = PlanFactory(offering=self.offering)
        self.resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.offering_user_1 = models.OfferingUser.objects.create(
            user=self.user,
            offering=self.offering,
        )
        self.offering_user_2 = models.OfferingUser.objects.create(
            user=self.other_user,
            offering=self.offering,
        )

        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        self.list_url = "/api/marketplace-offering-users/"

        from waldur_mastermind.marketplace.views import OfferingUsersViewSet

        self.viewset = OfferingUsersViewSet()
        self.viewset.queryset = models.OfferingUser.objects.all()

    def test_offering_users_queryset_query_optimization(self):
        """Test that OfferingUsersViewSet.get_queryset uses optimized queries."""

        factory = RequestFactory()
        request = factory.get("/api/marketplace-offering-users/")
        request.user = self.service_provider_user

        self.viewset.request = request
        self.viewset.action = "list"

        with override_settings(DEBUG=True):
            connection.queries.clear()

            queryset = self.viewset.get_queryset()

            query_count = len(connection.queries)

            self.assertLessEqual(query_count, 3)

            offering_users = list(queryset)
            self.assertEqual(len(offering_users), 1)
            self.assertEqual(offering_users[0].user, self.user)

    @override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False)
    def test_offering_users_queryset_query_optimization_without_tos(self):
        """Test query optimization when ToS enforcement is disabled."""

        factory = RequestFactory()
        request = factory.get("/api/marketplace-offering-users/")
        request.user = self.service_provider_user

        self.viewset.request = request
        self.viewset.action = "list"

        with override_settings(DEBUG=True):
            connection.queries.clear()

            queryset = self.viewset.get_queryset()

            query_count = len(connection.queries)
            self.assertLessEqual(query_count, 3)

            offering_users = list(queryset)
            self.assertEqual(len(offering_users), 2)

    def test_offering_users_queryset_query_optimization_staff_user(self):
        """Test query optimization for staff users (bypasses complex filtering)."""

        staff_user = UserFactory(is_staff=True)

        factory = RequestFactory()
        request = factory.get("/api/marketplace-offering-users/")
        request.user = staff_user

        self.viewset.request = request
        self.viewset.action = "list"

        with override_settings(DEBUG=True):
            connection.queries.clear()

            queryset = self.viewset.get_queryset()

            query_count = len(connection.queries)

            self.assertLessEqual(query_count, 2)

            offering_users = list(queryset)
            self.assertEqual(len(offering_users), 2)

    def test_offering_users_queryset_query_optimization_regular_user(self):
        """Test query optimization for regular users (sees only own records)."""

        factory = RequestFactory()
        request = factory.get("/api/marketplace-offering-users/")
        request.user = self.user

        self.viewset.request = request
        self.viewset.action = "list"

        with override_settings(DEBUG=True):
            connection.queries.clear()

            queryset = self.viewset.get_queryset()

            query_count = len(connection.queries)
            self.assertLessEqual(query_count, 3)

            offering_users = list(queryset)
            self.assertEqual(len(offering_users), 1)
            self.assertEqual(offering_users[0].user, self.user)


class OfferingTermsOfServiceFilterTest(APITransactionTestCase):
    """Test the has_active_terms_of_service filter for offerings."""

    def setUp(self):
        self.user = UserFactory()
        self.customer = CustomerFactory()
        self.category = CategoryFactory()

        self.offering_with_active_tos = OfferingFactory(
            customer=self.customer,
            category=self.category,
            name="Offering with active ToS",
            shared=True,
            state=OfferingStates.ACTIVE,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
            },
        )

        self.offering_with_inactive_tos = OfferingFactory(
            customer=self.customer,
            category=self.category,
            name="Offering with inactive ToS",
            shared=True,
            state=OfferingStates.ACTIVE,
        )

        self.offering_with_no_tos = OfferingFactory(
            customer=self.customer,
            category=self.category,
            name="Offering with no ToS",
            shared=True,
            state=OfferingStates.ACTIVE,
        )

        self.tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering_with_active_tos,
            terms_of_service="Active Terms of Service",
            version="1.0",
            is_active=True,
        )

        models.OfferingTermsOfService.objects.create(
            offering=self.offering_with_inactive_tos,
            terms_of_service="Inactive Terms of Service",
            version="1.0",
            is_active=False,
        )

        self.url = reverse("marketplace-public-offering-list")
        self.client.force_authenticate(user=self.user)

    def test_filter_offerings_with_active_terms_of_service_true(self):
        """Test filtering offerings that have active Terms of Service."""
        response = self.client.get(self.url, {"has_active_terms_of_service": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_active_tos.uuid.hex
        )
        self.assertEqual(response.data[0]["name"], "Offering with active ToS")

    def test_filter_offerings_with_active_terms_of_service_false(self):
        """Test filtering offerings that do not have active Terms of Service."""
        response = self.client.get(self.url, {"has_active_terms_of_service": "false"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        offering_uuids = [offering["uuid"] for offering in response.data]
        self.assertIn(self.offering_with_inactive_tos.uuid.hex, offering_uuids)
        self.assertIn(self.offering_with_no_tos.uuid.hex, offering_uuids)

    def test_filter_offerings_with_terms_of_service_without_filter(self):
        """Test that all offerings are returned when no filter is applied."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

        offering_uuids = [offering["uuid"] for offering in response.data]
        self.assertIn(self.offering_with_active_tos.uuid.hex, offering_uuids)
        self.assertIn(self.offering_with_inactive_tos.uuid.hex, offering_uuids)
        self.assertIn(self.offering_with_no_tos.uuid.hex, offering_uuids)

    def test_filter_offerings_with_terms_of_service_inactive_config(self):
        """Test that offerings with only inactive ToS configs are not included."""
        response = self.client.get(self.url, {"has_active_terms_of_service": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_active_tos.uuid.hex
        )

    def test_filter_offerings_with_terms_of_service_multiple_configs(self):
        """Test that offerings with multiple ToS configs (only one active) work correctly."""
        models.OfferingTermsOfService.objects.create(
            offering=self.offering_with_active_tos,
            terms_of_service="Another Terms of Service",
            version="2.0",
            is_active=False,
        )

        response = self.client.get(self.url, {"has_active_terms_of_service": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_active_tos.uuid.hex
        )

    def test_filter_offerings_with_terms_of_service_true(self):
        """Test filtering offerings that have any Terms of Service using has_terms_of_service filter."""
        response = self.client.get(self.url, {"has_terms_of_service": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        offering_uuids = [offering["uuid"] for offering in response.data]
        self.assertIn(self.offering_with_active_tos.uuid.hex, offering_uuids)
        self.assertIn(self.offering_with_inactive_tos.uuid.hex, offering_uuids)

    def test_filter_offerings_with_terms_of_service_false(self):
        """Test filtering offerings that do not have any Terms of Service using has_terms_of_service filter."""
        response = self.client.get(self.url, {"has_terms_of_service": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        self.assertEqual(response.data[0]["uuid"], self.offering_with_no_tos.uuid.hex)
        self.assertEqual(response.data[0]["name"], "Offering with no ToS")

    def test_combined_filters_active_tos_and_any_tos(self):
        """Test combined filters: has_active_terms_of_service=true AND has_terms_of_service=true."""
        response = self.client.get(
            self.url,
            {"has_active_terms_of_service": "true", "has_terms_of_service": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_active_tos.uuid.hex
        )

    def test_combined_filters_no_active_tos_but_has_any_tos(self):
        """Test combined filters: has_active_terms_of_service=false AND has_terms_of_service=true."""
        response = self.client.get(
            self.url,
            {"has_active_terms_of_service": "false", "has_terms_of_service": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_inactive_tos.uuid.hex
        )

    def test_combined_filters_no_active_tos_and_no_any_tos(self):
        """Test combined filters: has_active_terms_of_service=false AND has_terms_of_service=false."""
        response = self.client.get(
            self.url,
            {"has_active_terms_of_service": "false", "has_terms_of_service": "false"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering_with_no_tos.uuid.hex)

    def test_combined_filters_active_tos_but_no_any_tos_impossible(self):
        """Test combined filters: has_active_terms_of_service=true AND has_terms_of_service=false (should return empty)."""
        response = self.client.get(
            self.url,
            {"has_active_terms_of_service": "true", "has_terms_of_service": "false"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_filter_with_category_and_terms_of_service(self):
        """Test combining ToS filters with other filters like category."""
        response = self.client.get(
            self.url,
            {
                "category_uuid": str(self.category.uuid),
                "has_active_terms_of_service": "true",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_active_tos.uuid.hex
        )

    def test_filter_with_name_and_terms_of_service(self):
        """Test combining ToS filters with name filtering."""
        response = self.client.get(
            self.url,
            {"name": "Offering with active ToS", "has_terms_of_service": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_active_tos.uuid.hex
        )

    def test_filter_offerings_user_has_consent_true(self):
        """Test filtering offerings where user has consent."""
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering_with_active_tos,
            version="1.0",
        )

        response = self.client.get(self.url, {"user_has_consent": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["uuid"], self.offering_with_active_tos.uuid.hex
        )
        self.assertTrue(response.data[0]["user_has_consent"])

    def test_filter_offerings_user_has_consent_false(self):
        """Test filtering offerings where user does not have consent."""

        response = self.client.get(self.url, {"user_has_consent": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)
        for offering in response.data:
            self.assertFalse(offering["user_has_consent"])


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class TermsOfServiceConsentEventLoggingTest(APITransactionTestCase):
    """Test event logging for Terms of Service consent operations."""

    def setUp(self):
        self.user = UserFactory()
        self.customer = CustomerFactory()
        self.project = ProjectFactory(customer=self.customer)
        self.category = CategoryFactory()
        self.offering = OfferingFactory(
            category=self.category,
            customer=self.customer,
            type="Marketplace.Basic",
        )

        self.tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test Terms of Service",
            version="1.0",
            is_active=True,
        )

        self.project.add_user(self.user, role=ProjectRole.MANAGER)
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.consent_list_url = reverse("marketplace-user-offering-consent-list")
        self.consent_data = {
            "offering": self.offering.uuid,
        }
        Event.objects.all().delete()

    def _create_consent(self, user=None, offering=None, version="1.0"):
        """Helper method to create consent."""
        return models.UserOfferingConsent.objects.create(
            user=user or self.user,
            offering=offering or self.offering,
            version=version,
        )

    def _assert_event_created(self, event_type, count=1):
        """Helper method to assert event was created."""
        events = Event.objects.filter(event_type=event_type)
        self.assertEqual(events.count(), count)
        return events.first()

    def _assert_event_context(
        self, event, user_name=None, offering_name=None, version="1.0"
    ):
        """Helper method to assert event context."""
        if user_name is not None:
            self.assertEqual(event.context["user_name"], user_name)
        if offering_name is not None:
            self.assertEqual(event.context["offering_name"], offering_name)
        self.assertEqual(event.context["version"], version)

    def test_consent_granted_event_logging_via_api(self):
        """Test that consent granted event is logged when creating consent via API."""
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.consent_list_url, self.consent_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        event = self._assert_event_created("terms_of_service_consent_granted")
        self._assert_event_context(event, self.user.full_name, self.offering.name)

    def test_consent_granted_event_logging_direct_creation(self):
        """Test that consent granted event is logged when creating consent directly."""
        self._create_consent()
        event = self._assert_event_created("terms_of_service_consent_granted")
        self._assert_event_context(event, self.user.full_name, self.offering.name)

    def test_consent_revoked_event_logging_via_api(self):
        """Test that consent revoked event is logged when revoking consent via API."""
        self.client.force_authenticate(user=self.user)
        consent = self._create_consent()

        url = reverse(
            "marketplace-user-offering-consent-revoke",
            kwargs={"uuid": consent.uuid},
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        event = self._assert_event_created("terms_of_service_consent_revoked")
        self._assert_event_context(event, self.user.full_name, self.offering.name)

    def test_consent_revoked_event_logging_direct_revoke(self):
        """Test that consent revoked event is logged when revoking consent directly."""
        consent = self._create_consent()
        consent.revoke()

        event = self._assert_event_created("terms_of_service_consent_revoked")
        self._assert_event_context(event, self.user.full_name, self.offering.name)

    def test_no_duplicate_events_on_multiple_saves(self):
        """Test that no duplicate events are created on multiple saves."""
        self.client.force_authenticate(user=self.user)
        consent = self._create_consent()
        consent.save()
        self._assert_event_created("terms_of_service_consent_granted")

    def test_consent_revoked_event_context_includes_revocation_date(self):
        """Test that consent revoked event context includes revocation date."""
        consent = self._create_consent()
        consent.revoke()

        event = Event.objects.filter(
            event_type="terms_of_service_consent_revoked"
        ).first()
        self.assertIsNotNone(event.context["revocation_date"])
        self.assertIsNotNone(consent.revocation_date)

    def test_consent_granted_event_logging_with_username_fallback(self):
        """Test that consent granted event uses username when full_name is not available."""
        user_no_full_name = UserFactory(full_name="")
        self.project.add_user(user_no_full_name, role=ProjectRole.MANAGER)

        self._create_consent(user=user_no_full_name)
        event = self._assert_event_created("terms_of_service_consent_granted")
        self._assert_event_context(
            event, user_no_full_name.username, self.offering.name
        )

    def test_consent_granted_event_logging_multiple_consents(self):
        """Test that multiple consent granted events are logged for different users."""
        other_user = UserFactory()
        self.project.add_user(other_user, role=ProjectRole.MEMBER)

        self._create_consent()
        self._create_consent(user=other_user)

        events = Event.objects.filter(event_type="terms_of_service_consent_granted")
        self.assertEqual(events.count(), 2)

        user_names = [event.context["user_name"] for event in events]
        self.assertIn(self.user.full_name, user_names)
        self.assertIn(other_user.full_name, user_names)

    def test_consent_granted_event_logging_context_completeness(self):
        """Test that consent granted event context includes all required fields."""
        consent = self._create_consent()
        event = self._assert_event_created("terms_of_service_consent_granted")
        context = event.context

        self.assertIn("user_uuid", context)
        self.assertIn("user_name", context)
        self.assertIn("offering_uuid", context)
        self.assertIn("offering_name", context)
        self.assertIn("consent_uuid", context)
        self.assertIn("consent_version", context)
        self.assertIn("consent_agreement_date", context)
        self.assertIn("consent_revocation_date", context)
        self.assertIn("version", context)

        self.assertEqual(context["user_uuid"], self.user.uuid.hex)
        self.assertEqual(context["user_name"], self.user.full_name)
        self.assertEqual(context["offering_uuid"], self.offering.uuid.hex)
        self.assertEqual(context["offering_name"], self.offering.name)
        self.assertEqual(context["consent_uuid"], consent.uuid.hex)
        self.assertEqual(context["consent_version"], "1.0")
        self.assertEqual(context["version"], "1.0")

    def test_consent_granted_event_logging_message_template(self):
        """Test that consent granted event has correct message template."""
        self._create_consent()
        event = self._assert_event_created("terms_of_service_consent_granted")

        expected_message = f"User {self.user.full_name} has accepted Terms of Service for offering {self.offering.name}."
        self.assertEqual(event.message, expected_message)

    def test_consent_granted_event_logging_event_type(self):
        """Test that consent granted event has correct event type."""
        self._create_consent()
        event = self._assert_event_created("terms_of_service_consent_granted")
        self.assertEqual(event.event_type, "terms_of_service_consent_granted")

    def test_consent_granted_event_logging_via_events_api(self):
        """Test that consent granted event is accessible via events API endpoint."""
        self.client.force_authenticate(UserFactory(is_staff=True))
        self._create_consent()

        response = self.client.get(
            "/api/events/", {"event_type": "terms_of_service_consent_granted"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        event_data = response.data[0]
        self.assertEqual(event_data["event_type"], "terms_of_service_consent_granted")
        self.assertEqual(event_data["context"]["user_name"], self.user.full_name)
        self.assertEqual(event_data["context"]["offering_name"], self.offering.name)
        self.assertEqual(event_data["context"]["version"], "1.0")
