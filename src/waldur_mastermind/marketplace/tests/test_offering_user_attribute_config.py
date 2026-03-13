"""
Tests for OfferingUserAttributeConfig - per-offering user attribute exposure.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class OfferingUserAttributeConfigModelTest(test.APITestCase):
    """Test OfferingUserAttributeConfig model."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    def test_get_exposed_fields_returns_enabled_fields(self):
        """Test that get_exposed_fields returns only enabled fields."""
        config = models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=False,
            expose_nationality=True,
        )

        exposed = config.get_exposed_fields()

        self.assertIn("username", exposed)
        self.assertIn("full_name", exposed)
        self.assertIn("email", exposed)
        self.assertIn("nationality", exposed)
        self.assertNotIn("phone_number", exposed)

    def test_get_exposed_fields_for_offering_with_config(self):
        """Test get_exposed_fields_for_offering with explicit config."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=False,
            expose_email=True,
            expose_gender=True,
        )

        exposed = models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(
            self.offering
        )

        self.assertIn("username", exposed)
        self.assertIn("email", exposed)
        self.assertIn("gender", exposed)
        self.assertNotIn("full_name", exposed)

    def test_get_exposed_fields_for_offering_falls_back_to_constance(self):
        """Test that get_exposed_fields_for_offering falls back to Constance default."""
        # No config created for this offering

        with mock.patch(
            "waldur_mastermind.marketplace.models.constance_config"
        ) as mock_config:
            mock_config.DEFAULT_OFFERING_USER_ATTRIBUTES = [
                "username",
                "email",
                "organization",
            ]

            exposed = (
                models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(
                    self.offering
                )
            )

        self.assertEqual(exposed, ["username", "email", "organization"])

    def test_get_exposed_fields_for_offering_hardcoded_fallback(self):
        """Test hardcoded fallback when Constance is not configured."""
        # No config created for this offering

        with mock.patch(
            "waldur_mastermind.marketplace.models.constance_config"
        ) as mock_config:
            mock_config.DEFAULT_OFFERING_USER_ATTRIBUTES = None

            exposed = (
                models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(
                    self.offering
                )
            )

        self.assertEqual(exposed, ["username", "full_name", "email"])


class OfferingUserAttributeConfigAPITest(test.APITestCase):
    """Test OfferingUserAttributeConfig API endpoints under ProviderOfferingViewSet."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.get_url = f"/api/marketplace-provider-offerings/{self.offering.uuid}/user-attribute-config/"
        self.update_url = f"/api/marketplace-provider-offerings/{self.offering.uuid}/update-user-attribute-config/"
        self.delete_url = f"/api/marketplace-provider-offerings/{self.offering.uuid}/delete-user-attribute-config/"

    def test_staff_can_get_config(self):
        """Test that staff can get config for an offering."""
        config = models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_nationality=True,
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.get_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(config.uuid))
        self.assertTrue(response.data["expose_nationality"])

    def test_get_config_returns_defaults_when_not_exists(self):
        """Test that get returns default values when config doesn't exist."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.get_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("is_default"))
        self.assertIn("exposed_fields", response.data)

    def test_customer_owner_can_get_config(self):
        """Test that customer owner can get config for their offering."""
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
        )

        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.get(self.get_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_create_config(self):
        """Test that staff can create a config via update endpoint."""
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            self.update_url,
            {
                "expose_username": True,
                "expose_full_name": True,
                "expose_email": True,
                "expose_nationality": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["expose_nationality"])
        self.assertTrue(
            models.OfferingUserAttributeConfig.objects.filter(
                offering=self.offering
            ).exists()
        )

    def test_customer_owner_can_create_config(self):
        """Test that customer owner can create config for their offering."""
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        self.client.force_authenticate(user=self.fixture.user)

        response = self.client.post(
            self.update_url,
            {
                "expose_username": True,
                "expose_email": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.OfferingUserAttributeConfig.objects.filter(
                offering=self.offering
            ).exists()
        )

    def test_non_owner_cannot_create_config(self):
        """Test that non-owner cannot create config."""
        # User is not owner of the customer
        self.client.force_authenticate(user=self.fixture.user)

        response = self.client.post(
            self.update_url,
            {
                "expose_username": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_config_response_includes_exposed_fields_list(self):
        """Test that config response includes computed exposed_fields list."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_nationality=True,
            expose_gender=False,
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.get_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("exposed_fields", response.data)
        self.assertIn("username", response.data["exposed_fields"])
        self.assertIn("nationality", response.data["exposed_fields"])
        self.assertNotIn("gender", response.data["exposed_fields"])

    def test_customer_owner_can_update_config(self):
        """Test that customer owner can update config."""
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_nationality=False,
        )

        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.patch(
            self.update_url,
            {"expose_nationality": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config = models.OfferingUserAttributeConfig.objects.get(offering=self.offering)
        self.assertTrue(config.expose_nationality)

    def test_customer_owner_can_delete_config(self):
        """Test that customer owner can delete config."""
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        config = models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
        )

        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.delete(self.delete_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.OfferingUserAttributeConfig.objects.filter(pk=config.pk).exists()
        )

    def test_delete_succeeds_even_when_no_config(self):
        """Test that delete returns 204 even when no config exists (idempotent)."""
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        self.client.force_authenticate(user=self.fixture.user)

        response = self.client.delete(self.delete_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class OfferingTermsOfServiceCollectedAttributesTest(test.APITestCase):
    """Test collected_attributes property on OfferingTermsOfService."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    def test_collected_attributes_from_config(self):
        """Test that collected_attributes uses config when available."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_email=True,
            expose_nationality=True,
            expose_full_name=False,
        )

        tos = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test terms",
            is_active=True,
        )

        collected = tos.collected_attributes

        self.assertIn("username", collected)
        self.assertIn("email", collected)
        self.assertIn("nationality", collected)
        self.assertNotIn("full_name", collected)

    def test_collected_attributes_falls_back_to_default(self):
        """Test that collected_attributes falls back to default when no config."""
        tos = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test terms",
            is_active=True,
        )

        with mock.patch(
            "waldur_mastermind.marketplace.models.constance_config"
        ) as mock_config:
            mock_config.DEFAULT_OFFERING_USER_ATTRIBUTES = ["username", "email"]
            collected = tos.collected_attributes

        self.assertEqual(collected, ["username", "email"])


class UserOfferingConsentCollectedAttributesTest(test.APITestCase):
    """Test collected_attributes in UserOfferingConsent API."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.url = "/api/marketplace-user-offering-consents/"

    def test_consent_response_includes_collected_attributes(self):
        """Test that consent API response includes collected_attributes."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_email=True,
            expose_gender=True,
        )

        consent = models.UserOfferingConsent.objects.create(
            user=self.fixture.user,
            offering=self.offering,
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{consent.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("collected_attributes", response.data)
        self.assertIn("username", response.data["collected_attributes"])
        self.assertIn("email", response.data["collected_attributes"])
        self.assertIn("gender", response.data["collected_attributes"])


class OfferingUserSerializerAttributeFilteringTest(test.APITestCase):
    """Test that OfferingUserSerializer filters user attributes based on config.

    All user_* fields are defined in the schema for SDK generation.
    At runtime, fields are filtered based on OfferingUserAttributeConfig.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        self.url = "/api/marketplace-offering-users/"

    def _create_offering_user(self):
        """Create an OfferingUser for testing."""
        return models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.fixture.user,
            username="test_user",
        )

    def test_default_config_exposes_username_full_name_email(self):
        """Test that default config exposes username, full_name, email."""
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Default exposed fields
        self.assertIn("user_username", response.data)
        self.assertIn("user_full_name", response.data)
        self.assertIn("user_email", response.data)
        # Non-default fields should NOT be present
        self.assertNotIn("user_phone_number", response.data)
        self.assertNotIn("user_organization", response.data)
        self.assertNotIn("user_gender", response.data)
        self.assertNotIn("user_nationality", response.data)

    def test_config_with_extended_attributes_exposes_them(self):
        """Test that config with extended attributes exposes them."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=True,
            expose_organization=True,
            expose_nationality=True,
        )
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Core fields
        self.assertIn("user_username", response.data)
        self.assertIn("user_full_name", response.data)
        self.assertIn("user_email", response.data)
        # Extended fields that are enabled
        self.assertIn("user_phone_number", response.data)
        self.assertIn("user_organization", response.data)
        self.assertIn("user_nationality", response.data)
        # Fields that are NOT enabled
        self.assertNotIn("user_gender", response.data)
        self.assertNotIn("user_civil_number", response.data)

    def test_config_disabling_core_fields_hides_them(self):
        """Test that disabling core fields in config hides them."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=False,  # Disabled
            expose_email=False,  # Disabled
        )
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Only username is exposed
        self.assertIn("user_username", response.data)
        # Full name and email are hidden
        self.assertNotIn("user_full_name", response.data)
        self.assertNotIn("user_email", response.data)

    def test_extended_profile_attributes_exposed_when_configured(self):
        """Test that extended profile attributes are exposed when enabled in config."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_gender=True,
            expose_nationality=True,
            expose_eduperson_assurance=True,
            expose_organization_type=True,
        )
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Extended profile fields that are enabled
        self.assertIn("user_gender", response.data)
        self.assertIn("user_nationality", response.data)
        self.assertIn("user_eduperson_assurance", response.data)
        self.assertIn("user_organization_type", response.data)
        # Extended profile fields that are NOT enabled
        self.assertNotIn("user_place_of_birth", response.data)
        self.assertNotIn("user_personal_title", response.data)

    def test_sensitive_attributes_hidden_by_default(self):
        """Test that sensitive attributes like civil_number are hidden by default."""
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Sensitive fields should NOT be present by default
        self.assertNotIn("user_civil_number", response.data)
        self.assertNotIn("user_birth_date", response.data)
        self.assertNotIn("user_registration_method", response.data)

    def test_sensitive_attributes_exposed_when_explicitly_enabled(self):
        """Test that sensitive attributes are exposed when explicitly enabled."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_civil_number=True,  # Explicitly enabled
            expose_birth_date=True,  # Explicitly enabled
            expose_identity_source=True,  # Explicitly enabled
        )
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Sensitive fields are now exposed
        self.assertIn("user_civil_number", response.data)
        self.assertIn("user_birth_date", response.data)
        self.assertIn("user_identity_source", response.data)

    def test_active_isds_exposed_when_enabled(self):
        """Test that active_isds is exposed when expose_active_isds=True."""
        self.fixture.user.active_isds = ["isd:puhuri"]
        self.fixture.user.save()

        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_active_isds=True,
        )
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("user_active_isds", response.data)
        self.assertEqual(response.data["user_active_isds"], ["isd:puhuri"])

    def test_active_isds_hidden_by_default(self):
        """Test that active_isds is hidden by default."""
        self.fixture.user.active_isds = ["isd:puhuri"]
        self.fixture.user.save()

        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("user_active_isds", response.data)

    def test_organization_registry_code_exposed_when_enabled(self):
        """Test that organization_registry_code is exposed when enabled."""
        self.fixture.user.organization_registry_code = "12345678"
        self.fixture.user.save()

        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_organization_registry_code=True,
        )
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("user_organization_registry_code", response.data)
        self.assertEqual(response.data["user_organization_registry_code"], "12345678")

    def test_organization_registry_code_hidden_by_default(self):
        """Test that organization_registry_code is hidden by default."""
        self.fixture.user.organization_registry_code = "12345678"
        self.fixture.user.save()

        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{self.url}{offering_user.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("user_organization_registry_code", response.data)


class OfferingUserListViewAttributeFilteringTest(test.APITestCase):
    """Test that OfferingUserSerializer filters attributes consistently in list views.

    This is critical for PII protection - list views must apply the same filtering
    as detail views, respecting per-offering OfferingUserAttributeConfig.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        self.url = "/api/marketplace-offering-users/"

    def _create_offering_user(self, offering=None, user=None):
        """Create an OfferingUser for testing."""
        return models.OfferingUser.objects.create(
            offering=offering or self.offering,
            user=user or self.fixture.user,
            username="test_user",
        )

    def test_list_view_applies_attribute_filtering(self):
        """Test that list view filters attributes the same as detail view."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=False,  # Email explicitly disabled
            expose_phone_number=False,
        )
        self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)

        # Check the first result in the list
        offering_user_data = response.data[0]

        # Exposed fields should be present
        self.assertIn("user_username", offering_user_data)
        self.assertIn("user_full_name", offering_user_data)

        # Non-exposed fields should NOT be present
        self.assertNotIn("user_email", offering_user_data)
        self.assertNotIn("user_phone_number", offering_user_data)
        self.assertNotIn("user_nationality", offering_user_data)

    def test_list_view_default_config_matches_detail_view(self):
        """Test that list view with default config matches detail view behavior."""
        offering_user = self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)

        # Get detail view
        detail_response = self.client.get(f"{self.url}{offering_user.uuid}/")
        # Get list view
        list_response = self.client.get(self.url)

        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)

        list_data = list_response.data[0]

        # Both should have the same fields for user attributes
        user_attribute_fields = [
            "user_username",
            "user_full_name",
            "user_email",
            "user_phone_number",
            "user_organization",
            "user_nationality",
            "user_civil_number",
        ]

        for field in user_attribute_fields:
            detail_has_field = field in detail_response.data
            list_has_field = field in list_data
            self.assertEqual(
                detail_has_field,
                list_has_field,
                f"Field '{field}' presence differs: detail={detail_has_field}, list={list_has_field}",
            )

    def test_list_view_with_multiple_offerings_filters_per_offering(self):
        """Test that list view applies different configs for different offerings."""
        # Create second offering with different customer
        fixture2 = structure_fixtures.ProjectFixture()
        offering2 = factories.OfferingFactory(customer=fixture2.customer)

        # Offering 1: expose only username
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=False,
            expose_email=False,
        )

        # Offering 2: expose username, full_name, and email
        models.OfferingUserAttributeConfig.objects.create(
            offering=offering2,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
        )

        offering_user1 = self._create_offering_user(offering=self.offering)
        offering_user2 = self._create_offering_user(
            offering=offering2, user=fixture2.user
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Find data for each offering user
        data_by_uuid = {item["uuid"]: item for item in response.data}

        user1_data = data_by_uuid[str(offering_user1.uuid)]
        user2_data = data_by_uuid[str(offering_user2.uuid)]

        # Offering 1: only username should be present
        self.assertIn("user_username", user1_data)
        self.assertNotIn("user_full_name", user1_data)
        self.assertNotIn("user_email", user1_data)

        # Offering 2: username, full_name, and email should be present
        self.assertIn("user_username", user2_data)
        self.assertIn("user_full_name", user2_data)
        self.assertIn("user_email", user2_data)

    def test_list_view_sensitive_fields_hidden_by_default(self):
        """Test that sensitive fields are hidden in list view by default."""
        self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_user_data = response.data[0]

        # Sensitive fields should NOT be present by default
        self.assertNotIn("user_civil_number", offering_user_data)
        self.assertNotIn("user_birth_date", offering_user_data)
        self.assertNotIn("user_registration_method", offering_user_data)
        self.assertNotIn("user_nationality", offering_user_data)

    def test_list_view_sensitive_fields_exposed_when_configured(self):
        """Test that sensitive fields are exposed in list view when configured."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_civil_number=True,
            expose_birth_date=True,
            expose_nationality=True,
        )
        self._create_offering_user()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_user_data = response.data[0]

        # Sensitive fields should be present when configured
        self.assertIn("user_civil_number", offering_user_data)
        self.assertIn("user_birth_date", offering_user_data)
        self.assertIn("user_nationality", offering_user_data)
