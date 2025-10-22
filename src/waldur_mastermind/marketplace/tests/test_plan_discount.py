from decimal import Decimal
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class PlanDiscountUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.plan = self.fixture.plan
        self.offering = self.plan.offering

        self.offering.components.all().delete()
        self.plan.components.all().delete()

        # Create offering components
        self.cpu_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            name="CPU",
        )
        self.ram_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            name="RAM",
        )

        # Create plan components
        self.cpu_plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.cpu_component,
            price=Decimal("10.00"),
        )
        self.ram_plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.ram_component,
            price=Decimal("5.00"),
        )

        self.url = factories.PlanFactory.get_url(self.plan, "update_discounts")
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)

    def test_service_provider_can_update_discounts(self):
        """Service provider with UPDATE_OFFERING_PLAN permission can update discounts."""
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 10,
                    "discount_rate": 15,
                },
                "ram": {
                    "discount_threshold": 100,
                    "discount_rate": 20,
                },
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify CPU component was updated
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(self.cpu_plan_component.discount_threshold, 10)
        self.assertEqual(self.cpu_plan_component.discount_rate, 15)

        # Verify RAM component was updated
        self.ram_plan_component.refresh_from_db()
        self.assertEqual(self.ram_plan_component.discount_threshold, 100)
        self.assertEqual(self.ram_plan_component.discount_rate, 20)

    def test_staff_can_update_discounts(self):
        """Staff user can update discounts."""
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 5,
                    "discount_rate": 10,
                }
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(self.cpu_plan_component.discount_threshold, 5)
        self.assertEqual(self.cpu_plan_component.discount_rate, 10)

    def test_customer_without_permission_cannot_update_discounts(self):
        """Customer without UPDATE_OFFERING_PLAN permission cannot update discounts."""
        user = self.fixture.owner
        self.client.force_authenticate(user)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 10,
                    "discount_rate": 15,
                }
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_remove_discount_by_setting_null(self):
        """Discounts can be removed by setting both fields to null."""
        # First set discounts
        self.cpu_plan_component.discount_threshold = 10
        self.cpu_plan_component.discount_rate = 15
        self.cpu_plan_component.save()

        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": None,
                    "discount_rate": None,
                }
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.cpu_plan_component.refresh_from_db()
        self.assertIsNone(self.cpu_plan_component.discount_threshold)
        self.assertIsNone(self.cpu_plan_component.discount_rate)

    def test_validation_error_when_only_threshold_provided(self):
        """Validation error when only discount_threshold is provided without discount_rate."""
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 10,
                }
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be provided together", str(response.data))

    def test_validation_error_when_only_rate_provided(self):
        """Validation error when only discount_rate is provided without discount_threshold."""
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_rate": 15,
                }
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be provided together", str(response.data))

    @data(
        {"discount_threshold": -5, "discount_rate": 15},
        {"discount_threshold": 0, "discount_rate": 15},
    )
    def test_validation_error_for_invalid_threshold(self, discount_config):
        """Validation error when discount_threshold is zero or negative."""
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {"discounts": {"cpu": discount_config}}

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(
        {"discount_threshold": 10, "discount_rate": -5},
        {"discount_threshold": 10, "discount_rate": 101},
        {"discount_threshold": 10, "discount_rate": 150},
    )
    def test_validation_error_for_invalid_rate(self, discount_config):
        """Validation error when discount_rate is outside 0-100 range."""
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {"discounts": {"cpu": discount_config}}

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validation_error_for_invalid_component_type(self):
        """Validation error when component type doesn't exist in offering."""
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "invalid_component": {
                    "discount_threshold": 10,
                    "discount_rate": 15,
                }
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid component types", str(response.data))

    def test_update_multiple_components_at_once(self):
        """Multiple components can be updated in a single request."""
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 10,
                    "discount_rate": 15,
                },
                "ram": {
                    "discount_threshold": 50,
                    "discount_rate": 25,
                },
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify both components were updated
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(self.cpu_plan_component.discount_threshold, 10)
        self.assertEqual(self.cpu_plan_component.discount_rate, 15)

        self.ram_plan_component.refresh_from_db()
        self.assertEqual(self.ram_plan_component.discount_threshold, 50)
        self.assertEqual(self.ram_plan_component.discount_rate, 25)

    def test_partial_update_only_affected_components(self):
        """Only specified components are updated, others remain unchanged."""
        # Set initial discounts for both components
        self.cpu_plan_component.discount_threshold = 5
        self.cpu_plan_component.discount_rate = 10
        self.cpu_plan_component.save()

        self.ram_plan_component.discount_threshold = 20
        self.ram_plan_component.discount_rate = 30
        self.ram_plan_component.save()

        self.client.force_authenticate(self.fixture.service_owner)

        # Only update CPU
        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 15,
                    "discount_rate": 20,
                }
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # CPU should be updated
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(self.cpu_plan_component.discount_threshold, 15)
        self.assertEqual(self.cpu_plan_component.discount_rate, 20)

        # RAM should remain unchanged
        self.ram_plan_component.refresh_from_db()
        self.assertEqual(self.ram_plan_component.discount_threshold, 20)
        self.assertEqual(self.ram_plan_component.discount_rate, 30)

    def test_no_changes_when_same_values_provided(self):
        """No database updates when provided values match existing values."""
        # Set initial discounts
        self.cpu_plan_component.discount_threshold = 10
        self.cpu_plan_component.discount_rate = 15
        self.cpu_plan_component.save()

        self.client.force_authenticate(self.fixture.service_owner)

        # Provide same values
        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 10,
                    "discount_rate": 15,
                }
            }
        }

        with mock.patch.object(models.PlanComponent, "save") as mock_save:
            response = self.client.post(self.url, payload)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            # Save should not be called since values haven't changed
            mock_save.assert_not_called()

    def test_boundary_values_for_discount_rate(self):
        """Discount rate accepts boundary values 0 and 100."""
        self.client.force_authenticate(self.fixture.service_owner)

        # Test 0%
        payload = {
            "discounts": {
                "cpu": {
                    "discount_threshold": 10,
                    "discount_rate": 0,
                }
            }
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(self.cpu_plan_component.discount_rate, 0)

        # Test 100%
        payload["discounts"]["cpu"]["discount_rate"] = 100

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(self.cpu_plan_component.discount_rate, 100)

    def test_empty_discounts_dictionary(self):
        """Empty discounts dictionary returns success without changes."""
        self.client.force_authenticate(self.fixture.staff)

        payload = {"discounts": {}}

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
