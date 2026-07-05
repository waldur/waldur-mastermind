from decimal import Decimal
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class PlanDiscountUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.plan = self.fixture.plan
        self.offering = self.plan.offering

        self.offering.components.all().delete()
        self.plan.components.all().delete()

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
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {"discount_formula": "15 if usage >= 10 else 0"},
                "ram": {"discount_formula": "20 if usage >= 100 else 0"},
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(
            self.cpu_plan_component.discount_formula, "15 if usage >= 10 else 0"
        )

        self.ram_plan_component.refresh_from_db()
        self.assertEqual(
            self.ram_plan_component.discount_formula, "20 if usage >= 100 else 0"
        )

    def test_service_provider_can_set_discount_scope(self):
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {
                    "discount_formula": "15 if usage >= 10 else 0",
                    "discount_aggregation": "resource",
                },
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(
            self.cpu_plan_component.discount_formula, "15 if usage >= 10 else 0"
        )
        self.assertEqual(self.cpu_plan_component.discount_aggregation, "resource")

    def test_staff_can_update_discounts(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "discounts": {"cpu": {"discount_formula": "10 if usage >= 5 else 0"}}
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(
            self.cpu_plan_component.discount_formula, "10 if usage >= 5 else 0"
        )

    def test_customer_without_permission_cannot_update_discounts(self):
        self.client.force_authenticate(self.fixture.owner)

        payload = {
            "discounts": {"cpu": {"discount_formula": "15 if usage >= 10 else 0"}}
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_remove_discount_by_setting_empty_formula(self):
        self.cpu_plan_component.discount_formula = "15 if usage >= 10 else 0"
        self.cpu_plan_component.save()

        self.client.force_authenticate(self.fixture.service_owner)

        payload = {"discounts": {"cpu": {"discount_formula": ""}}}

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(self.cpu_plan_component.discount_formula, "")

    @data(
        "LN(usage",  # syntax error
        "LN(usage)",  # domain error at usage=0
        "EXP(usage)",  # unknown function
        "price * 2",  # unknown variable
    )
    def test_validation_error_for_broken_formula(self, formula):
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {"discounts": {"cpu": {"discount_formula": formula}}}

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_complex_formula_is_accepted(self):
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {"discount_formula": "MIN(70, LN(MAX(1, usage)) * 10)"}
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(
            self.cpu_plan_component.discount_formula, "MIN(70, LN(MAX(1, usage)) * 10)"
        )

    def test_validation_error_for_invalid_component_type(self):
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "invalid_component": {"discount_formula": "15 if usage >= 10 else 0"}
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid component types", str(response.data))

    def test_update_multiple_components_at_once(self):
        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {
                "cpu": {"discount_formula": "15 if usage >= 10 else 0"},
                "ram": {"discount_formula": "25 if usage >= 50 else 0"},
            }
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(
            self.cpu_plan_component.discount_formula, "15 if usage >= 10 else 0"
        )

        self.ram_plan_component.refresh_from_db()
        self.assertEqual(
            self.ram_plan_component.discount_formula, "25 if usage >= 50 else 0"
        )

    def test_partial_update_only_affected_components(self):
        self.cpu_plan_component.discount_formula = "10 if usage >= 5 else 0"
        self.cpu_plan_component.save()
        self.ram_plan_component.discount_formula = "30 if usage >= 20 else 0"
        self.ram_plan_component.save()

        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {"cpu": {"discount_formula": "20 if usage >= 15 else 0"}}
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.cpu_plan_component.refresh_from_db()
        self.assertEqual(
            self.cpu_plan_component.discount_formula, "20 if usage >= 15 else 0"
        )

        self.ram_plan_component.refresh_from_db()
        self.assertEqual(
            self.ram_plan_component.discount_formula, "30 if usage >= 20 else 0"
        )

    def test_no_changes_when_same_values_provided(self):
        self.cpu_plan_component.discount_formula = "15 if usage >= 10 else 0"
        self.cpu_plan_component.save()

        self.client.force_authenticate(self.fixture.service_owner)

        payload = {
            "discounts": {"cpu": {"discount_formula": "15 if usage >= 10 else 0"}}
        }

        with mock.patch.object(models.PlanComponent, "save") as mock_save:
            response = self.client.post(self.url, payload)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            mock_save.assert_not_called()

    def test_empty_discounts_dictionary(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = {"discounts": {}}

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
