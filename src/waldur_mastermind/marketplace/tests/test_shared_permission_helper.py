"""Tests for the shared permission helper function get_allowed_offering_users_for_user."""

from django.test import TestCase
from rest_framework.test import APITestCase

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace.views import get_allowed_offering_users_for_user


class SharedPermissionHelperTest(APITestCase):
    """Test the shared permission helper function."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.user = self.fixture.user
        self.customer = self.fixture.customer

        # Create offerings with proper plugin options
        self.offering1 = factories.OfferingFactory(
            customer=self.customer,
            name="Test Offering 1",
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        self.offering2 = factories.OfferingFactory(
            customer=self.customer,
            name="Test Offering 2",
            plugin_options={"service_provider_can_create_offering_user": True},
        )

        # Create offering users
        self.offering_user1 = factories.OfferingUserFactory(
            offering=self.offering1, user=self.user, username="test_user1"
        )
        self.offering_user2 = factories.OfferingUserFactory(
            offering=self.offering2, user=self.user, username="test_user2"
        )

    def test_staff_user_sees_all_offering_users(self):
        """Test that staff users see all OfferingUsers without filtering."""
        staff_user = structure_fixtures.UserFixture().user
        staff_user.is_staff = True
        staff_user.save()

        queryset = get_allowed_offering_users_for_user(staff_user)

        # Staff should see all OfferingUsers
        self.assertGreaterEqual(queryset.count(), 2)
        offering_user_ids = list(queryset.values_list("id", flat=True))
        self.assertIn(self.offering_user1.id, offering_user_ids)
        self.assertIn(self.offering_user2.id, offering_user_ids)

    def test_support_user_sees_all_offering_users(self):
        """Test that support users see all OfferingUsers without filtering."""
        support_user = structure_fixtures.UserFixture().user
        support_user.is_support = True
        support_user.save()

        queryset = get_allowed_offering_users_for_user(support_user)

        # Support should see all OfferingUsers
        self.assertGreaterEqual(queryset.count(), 2)
        offering_user_ids = list(queryset.values_list("id", flat=True))
        self.assertIn(self.offering_user1.id, offering_user_ids)
        self.assertIn(self.offering_user2.id, offering_user_ids)

    def test_regular_user_sees_own_offering_users(self):
        """Test that regular users see their own OfferingUsers."""
        queryset = get_allowed_offering_users_for_user(self.user)

        # Should see own OfferingUsers
        offering_user_ids = list(queryset.values_list("id", flat=True))
        self.assertIn(self.offering_user1.id, offering_user_ids)
        self.assertIn(self.offering_user2.id, offering_user_ids)

    def test_service_provider_sees_managed_offering_users(self):
        """Test that service providers see OfferingUsers for offerings they manage."""
        sp_user = structure_fixtures.UserFixture().user
        self.customer.add_user(sp_user, CustomerRole.OWNER)

        queryset = get_allowed_offering_users_for_user(sp_user)

        # Should see OfferingUsers for managed offerings
        offering_user_ids = list(queryset.values_list("id", flat=True))
        self.assertIn(self.offering_user1.id, offering_user_ids)
        self.assertIn(self.offering_user2.id, offering_user_ids)

    def test_unrelated_user_sees_no_offering_users(self):
        """Test that unrelated users see no OfferingUsers."""
        unrelated_user = structure_fixtures.UserFixture().user

        queryset = get_allowed_offering_users_for_user(unrelated_user)

        # Should see no OfferingUsers
        offering_user_ids = list(queryset.values_list("id", flat=True))
        self.assertNotIn(self.offering_user1.id, offering_user_ids)
        self.assertNotIn(self.offering_user2.id, offering_user_ids)

    def test_offering_without_plugin_options_filtered_out(self):
        """Test that offerings without proper plugin options are filtered out."""
        # Create offering without the required plugin option
        offering_no_plugin = factories.OfferingFactory(
            customer=self.customer, name="No Plugin Offering"
        )
        offering_user_no_plugin = factories.OfferingUserFactory(
            offering=offering_no_plugin, user=self.user, username="no_plugin_user"
        )

        queryset = get_allowed_offering_users_for_user(self.user)

        # Should not see OfferingUser for offering without plugin options
        offering_user_ids = list(queryset.values_list("id", flat=True))
        self.assertNotIn(offering_user_no_plugin.id, offering_user_ids)

        # But should still see others
        self.assertIn(self.offering_user1.id, offering_user_ids)
        self.assertIn(self.offering_user2.id, offering_user_ids)

    def test_include_consent_filtering_parameter(self):
        """Test that consent filtering can be enabled/disabled."""
        # Test without consent filtering
        queryset_no_consent = get_allowed_offering_users_for_user(
            self.user, include_consent_filtering=False
        )

        # Test with consent filtering
        queryset_with_consent = get_allowed_offering_users_for_user(
            self.user, include_consent_filtering=True, action="list"
        )

        # Both should return results for this test setup
        self.assertGreater(queryset_no_consent.count(), 0)
        self.assertGreater(queryset_with_consent.count(), 0)

    def test_staff_user_ignores_consent_filtering(self):
        """Test that staff users ignore consent filtering even when enabled."""
        staff_user = structure_fixtures.UserFixture().user
        staff_user.is_staff = True
        staff_user.save()

        queryset = get_allowed_offering_users_for_user(
            staff_user, include_consent_filtering=True, action="list"
        )

        # Staff should see all OfferingUsers regardless of consent filtering
        self.assertGreaterEqual(queryset.count(), 2)


class SharedPermissionHelperIntegrationTest(TestCase):
    """Integration tests for the shared permission helper with both ViewSets."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.user = self.fixture.user

        # Create offering with checklist
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Test Offering",
            plugin_options={"service_provider_can_create_offering_user": True},
        )

        self.offering_user = factories.OfferingUserFactory(
            offering=self.offering, user=self.user, username="test_user"
        )

    def test_both_viewsets_use_same_permission_logic(self):
        """Test that both ViewSets return consistent results for the same user."""
        from rest_framework.test import APIRequestFactory

        from waldur_mastermind.marketplace.views import (
            OfferingUsersViewSet,
        )

        factory = APIRequestFactory()
        request = factory.get("/")
        request.user = self.user

        # Test OfferingUsersViewSet
        offering_users_viewset = OfferingUsersViewSet()
        offering_users_viewset.request = request
        offering_users_viewset.action = "list"
        offering_users_queryset = offering_users_viewset.get_queryset()

        # The helper function should return the same OfferingUsers
        helper_queryset = get_allowed_offering_users_for_user(
            self.user, include_consent_filtering=True, action="list"
        )

        # Both should return the same OfferingUsers
        offering_user_ids_viewset = set(
            offering_users_queryset.values_list("id", flat=True)
        )
        offering_user_ids_helper = set(helper_queryset.values_list("id", flat=True))

        self.assertEqual(offering_user_ids_viewset, offering_user_ids_helper)
        self.assertIn(self.offering_user.id, offering_user_ids_viewset)
