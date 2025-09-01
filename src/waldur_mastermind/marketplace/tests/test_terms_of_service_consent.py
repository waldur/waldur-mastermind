import unittest
import uuid

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITransactionTestCase

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
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates
from waldur_mastermind.marketplace.tests.factories import (
    CategoryFactory,
    OfferingFactory,
    OrderFactory,
    PlanFactory,
    ResourceFactory,
    ServiceProviderFactory,
)

User = get_user_model()


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

        # Create new ToS config that requires reconsent
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Updated Terms of Service",
            version="2.0",
            requires_reconsent=True,
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

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Updated Terms of Service",
            version="2.0",
            requires_reconsent=True,
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

    @unittest.skip("Temporarily disabled")
    def test_resource_access_after_consent_granted(self):
        """Test that resource access works when consent is granted after resource creation."""
        self.project.add_user(self.user, role=ProjectRole.MANAGER)

        # Create resource
        resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )
        resource.state = ResourceStates.OK
        resource.save()

        # Verify resource access is denied without consent
        self.client.force_authenticate(user=self.user)
        response = self.client.get(ResourceFactory.get_url(resource))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Grant consent
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

        # Check that resource access is now allowed
        response = self.client.get(ResourceFactory.get_url(resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @unittest.skip("Temporarily disabled")
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

    @unittest.skip("Temporarily disabled")
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

    @unittest.skip("Temporarily disabled")
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

    @unittest.skip("Temporarily disabled")
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

    @unittest.skip("Temporarily disabled")
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
        )
        self.tos_config = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Initial terms of service",
            terms_of_service_link="https://example.com/tos",
            version="1.0",
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

    @unittest.skip("Temporarily disabled")
    def test_resource_access_without_consent_denied(self):
        """Test that resource access is denied when user hasn't consented to ToS."""
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Terms of Service consent required", response.data["detail"])

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

    def test_resource_access_service_provider_bypass(self):
        """Test that service provider users or customer users with list_resources permission can access resources without ToS consent."""
        service_provider_user = UserFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)

        self.client.force_authenticate(user=service_provider_user)

        response = self.client.get(self.resource_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resource_access_no_tos_requirements(self):
        """Test that resource access is allowed when offering has no ToS requirements."""
        self.tos_config.delete()

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @unittest.skip("Temporarily disabled")
    def test_resource_access_revoked_consent_denied(self):
        """Test that resource access is denied when consent has been revoked."""
        consent = models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )
        consent.revoke()

        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.resource_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Terms of Service consent required", response.data["detail"])

    def test_resource_list_still_accessible(self):
        """Test that resource list is still accessible even without consent."""
        self.client.force_authenticate(user=self.user)

        response = self.client.get(ResourceFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(str(self.resource.name), str(response.data))


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
