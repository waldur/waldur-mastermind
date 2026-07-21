import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

import ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.utils import parse_date
from waldur_mastermind.invoices.models import PeriodMixin
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class InvoiceItemDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def delete_invoice_item(self, user):
        self.client.force_authenticate(user)
        return self.client.delete(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
        )

    def test_staff_can_delete_invoice_item(self):
        response = self.delete_invoice_item(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_non_staff_can_not_delete_invoice_item(self):
        response = self.delete_invoice_item(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.logging.event_logger.emit")
    def test_event_is_emitted(self, logger_mock):
        self.delete_invoice_item(self.fixture.staff)
        self.assertEqual(
            logger_mock.call_args[-1]["event_type"], "invoice_item_deleted"
        )


class InvoiceItemUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def update_invoice_item(self, user):
        self.client.force_authenticate(user)
        return self.client.patch(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
            {"article_code": "AA11"},
        )

    def test_staff_can_update_invoice_item(self):
        response = self.update_invoice_item(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.invoice_item.refresh_from_db()
        self.assertEqual("AA11", self.fixture.invoice_item.article_code)

    def test_non_staff_can_not_update_invoice_item(self):
        response = self.update_invoice_item(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.logging.event_logger.emit")
    def test_event_is_emitted(self, logger_mock):
        self.update_invoice_item(self.fixture.staff)
        self.assertEqual(
            logger_mock.call_args[-1]["event_type"], "invoice_item_updated"
        )

    def test_when_quantity_is_updated_component_usage_is_updated_too(self):
        # Arrange
        item = self.fixture.invoice_item
        resource = marketplace_factories.ResourceFactory()
        offering = resource.offering
        item.resource = resource
        offering_component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.USAGE,
        )
        plan = marketplace_factories.PlanFactory(
            offering=offering,
        )
        plan_component = marketplace_factories.PlanComponentFactory(
            plan=plan, component=offering_component
        )
        item.details["plan_component_id"] = plan_component.id
        item.save()
        billing_period = date(year=item.invoice.year, month=item.invoice.month, day=1)
        component_usage = marketplace_factories.ComponentUsageFactory(
            resource=resource,
            component=offering_component,
            billing_period=billing_period,
            usage=100,
        )

        # Act
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
            {"quantity": 200},
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        component_usage.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(component_usage.usage, 200)
        self.assertEqual(item.quantity, 200)

    def test_when_start_and_end_are_updated_quantity_is_updated_too(self):
        # Arrange
        item = self.fixture.invoice_item
        resource = marketplace_factories.ResourceFactory()
        offering = resource.offering
        item.resource = resource
        offering_component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.FIXED,
        )
        plan = marketplace_factories.PlanFactory(
            offering=offering, unit=marketplace_models.Plan.Units.PER_DAY
        )
        plan_component = marketplace_factories.PlanComponentFactory(
            plan=plan, component=offering_component
        )
        item.details["plan_component_id"] = plan_component.id
        item.start = parse_date("2022-02-01")
        item.end = parse_date("2022-02-28")
        item.quantity = 28
        item.save()

        # Act
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
            {"start": "2022-02-01T00:00:00", "end": "2022-02-07T00:00:00"},
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 6)


class InvoiceItemCompensationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.item = self.fixture.invoice_item

    def create_compensation(self, user, offering_component_name="Compensation"):
        self.client.force_authenticate(user)
        url = factories.InvoiceItemFactory.get_url(self.item, "create_compensation")
        return self.client.post(
            url, {"offering_component_name": offering_component_name}
        )

    def test_staff_can_create_compensation(self):
        response = self.create_compensation(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_new_invoice_item_has_valid_details(self):
        self.create_compensation(self.fixture.staff)
        new_invoice_item = self.fixture.invoice.items.last()
        self.assertEqual(
            str(new_invoice_item.details["original_invoice_item_uuid"]),
            str(self.item.uuid),
        )
        self.assertEqual(
            new_invoice_item.details["offering_component_name"], "Compensation"
        )

    def test_compensation_for_invoice_item_with_negative_price_is_invalid(self):
        self.item.unit_price *= -1
        self.item.save()
        response = self.create_compensation(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_can_not_create_compensation(self):
        response = self.create_compensation(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.logging.event_logger.emit")
    def test_event_is_emitted(self, logger_mock):
        self.create_compensation(self.fixture.staff)
        self.assertEqual(
            logger_mock.call_args[-1]["event_type"], "invoice_item_created"
        )


@ddt.ddt
@freeze_time("2019-01-01")
class InvoiceTerminateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.item = self.fixture.invoice_item

    def test_when_item_is_terminated_quantity_is_not_updated_if_component_is_not_defined(
        self,
    ):
        old_quantity = self.item.quantity
        self.item.terminate()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, old_quantity)

    def test_when_item_is_terminated_quantity_is_updated_if_component_is_fixed(self):
        self.item.details["plan_component_id"] = self.fixture.plan_component.id
        self.item.save()
        with freeze_time("2019-01-31"):
            self.item.terminate()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 30)

    @ddt.data(
        LimitPeriods.MONTH,
        LimitPeriods.QUARTERLY,
        LimitPeriods.ANNUAL,
    )
    def test_when_item_is_terminated_quantity_is_updated_if_component_is_month_or_annual_limit(
        self, limit_period
    ):
        self.fixture.offering_component.billing_type = BillingTypes.LIMIT
        self.fixture.offering_component.limit_period = limit_period
        self.fixture.offering_component.save()
        self.item.details["plan_component_id"] = self.fixture.plan_component.id
        self.item.save()
        with freeze_time("2019-01-31"):
            self.item.terminate()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 30)

    def test_when_item_is_terminated_quantity_is_not_updated_if_component_is_total_limit(
        self,
    ):
        old_quantity = self.item.quantity
        self.fixture.offering_component.billing_type = BillingTypes.LIMIT
        self.fixture.offering_component.limit_period = LimitPeriods.TOTAL
        self.fixture.offering_component.save()
        self.item.details["plan_component_id"] = self.fixture.plan_component.id
        self.item.save()
        with freeze_time("2019-01-31"):
            self.item.terminate()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, old_quantity)


@freeze_time("2019-01-01")
class InvoiceItemMigrateToTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.item = self.fixture.invoice_item
        self.target_invoice = factories.InvoiceFactory(
            customer=self.fixture.customer, month=12, year=2018
        )

    def test_user_can_migrate_item(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(self.item, "migrate_to")
        response = self.client.post(
            url, {"invoice": factories.InvoiceFactory.get_url(self.target_invoice)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_cannot_migrate_item_between_different_customers(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(self.item, "migrate_to")
        response = self.client.post(
            url, {"invoice": factories.InvoiceFactory.get_url()}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_migrate_item_if_it_already_in_target_invoice(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(self.item, "migrate_to")
        response = self.client.post(
            url, {"invoice": factories.InvoiceFactory.get_url(self.item)}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@freeze_time("2019-01-01")
class InvoiceItemCostsForPeriodTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice1 = factories.InvoiceFactory(
            customer=self.fixture.customer, month=12, year=2018
        )
        self.invoice2 = factories.InvoiceFactory(
            customer=self.fixture.customer, month=11, year=2018
        )
        self.invoice3 = factories.InvoiceFactory(
            customer=self.fixture.customer, month=5, year=2018
        )
        self.project2 = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        self.item1 = factories.InvoiceItemFactory(
            invoice=self.invoice1,
            project=self.fixture.project,
            unit_price=10,
            quantity=10,
        )
        self.item2 = factories.InvoiceItemFactory(
            invoice=self.invoice2,
            project=self.fixture.project,
            unit_price=20,
            quantity=5,
        )
        self.item3 = factories.InvoiceItemFactory(
            invoice=self.invoice3,
            project=self.fixture.project,
            unit_price=30,
            quantity=3,
        )
        self.item4 = factories.InvoiceItemFactory(
            invoice=self.invoice1,
            project=self.project2,
            unit_price=40,
            quantity=2,
        )

        self.project_costs_url = factories.InvoiceItemFactory.get_list_url(
            "project_costs_for_period"
        )
        self.customer_costs_url = factories.InvoiceItemFactory.get_list_url(
            "customer_costs_for_period"
        )
        self.random_uuid = uuid.uuid4().hex
        self.user = structure_factories.UserFactory()

    def test_project_costs_for_3_months_period(self):
        self.client.force_authenticate(self.fixture.staff)
        period = PeriodMixin.Periods.MONTH_3
        response = self.client.get(
            self.project_costs_url,
            {"project_uuid": self.fixture.project.uuid.hex, "period": period},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_price"], "200.00")

    def test_project_costs_for_total_period(self):
        self.client.force_authenticate(self.fixture.staff)
        period = PeriodMixin.Periods.TOTAL
        response = self.client.get(
            self.project_costs_url,
            {"project_uuid": self.fixture.project.uuid.hex, "period": period},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_price"], "290.00")

    def test_project_costs_filtered_by_resource(self):
        # Two resources in the project with different accrued costs.
        resource_a = marketplace_factories.ResourceFactory(project=self.fixture.project)
        resource_b = marketplace_factories.ResourceFactory(project=self.fixture.project)
        factories.InvoiceItemFactory(
            invoice=self.invoice1,
            project=self.fixture.project,
            resource=resource_a,
            unit_price=100,
            quantity=1,
        )
        factories.InvoiceItemFactory(
            invoice=self.invoice1,
            project=self.fixture.project,
            resource=resource_b,
            unit_price=7,
            quantity=1,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.project_costs_url,
            {
                "project_uuid": self.fixture.project.uuid.hex,
                "resource_uuid": resource_a.uuid.hex,
                "period": PeriodMixin.Periods.TOTAL,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Only resource_a's cost is counted, not resource_b or the unscoped items.
        self.assertEqual(response.data["total_price"], "100.00")

    def test_uuid_is_not_connected_to_any_project(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_list_url("project_costs_for_period")
        response = self.client.get(url, {"project_uuid": self.random_uuid})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_not_get_project_costs_for_period_if_not_connected(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(
            self.project_costs_url, {"project_uuid": self.fixture.project.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_customer_costs_for_3_months_period(self):
        self.client.force_authenticate(self.fixture.staff)
        period = PeriodMixin.Periods.MONTH_3
        response = self.client.get(
            self.customer_costs_url,
            {"customer_uuid": self.fixture.customer.uuid.hex, "period": period},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_price"], "280.00")

    def test_uuid_is_not_connected_to_any_customer(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_list_url("customer_costs_for_period")
        response = self.client.get(url, {"customer_uuid": self.random_uuid})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_not_get_customer_costs_for_period_if_not_connected(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(
            self.customer_costs_url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class InvoiceItemCostsTest(test.APITestCase):
    def setUp(self):
        self.url = factories.InvoiceItemFactory.get_list_url("costs")
        self.project = structure_factories.ProjectFactory()
        self.invoice = factories.InvoiceFactory()
        # Staff because these tests focus on the aggregation/filter logic
        # of the action; access control is exercised separately in
        # test_security_c5_idor_fix.py.
        self.user = structure_factories.UserFactory(is_staff=True)

    def test_costs_requires_project_uuid(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_costs_validates_project_uuid_format(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + "?project_uuid=invalid")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_costs_returns_empty_list_if_no_items(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + f"?project_uuid={self.project.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_costs_returns_aggregated_data(self):
        # Create invoice items with both positive and negative prices
        factories.InvoiceItemFactory(
            invoice=self.invoice, project=self.project, unit_price=100, quantity=1
        )
        factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=-50,  # Compensation
            quantity=1,
        )
        factories.InvoiceItemFactory(
            invoice=self.invoice, project=self.project, unit_price=75, quantity=2
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + f"?project_uuid={self.project.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        item = response.data[0]
        self.assertEqual(item["year"], self.invoice.year)
        self.assertEqual(item["month"], self.invoice.month)
        self.assertEqual(item["price"], "200.00")
        self.assertEqual(item["compensation"], "-50.00")
        self.assertEqual(item["incurred"], "250.00")

    def test_costs_respects_order(self):
        # Create items for different months
        old_invoice = factories.InvoiceFactory(year=2023, month=1)
        new_invoice = factories.InvoiceFactory(year=2023, month=2)

        factories.InvoiceItemFactory(
            invoice=old_invoice, project=self.project, unit_price=100, quantity=1
        )
        factories.InvoiceItemFactory(
            invoice=new_invoice, project=self.project, unit_price=200, quantity=1
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + f"?project_uuid={self.project.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Check if ordered by year and month in descending order
        self.assertEqual(response.data[0]["year"], 2023)
        self.assertEqual(response.data[0]["month"], 2)
        self.assertEqual(response.data[1]["year"], 2023)
        self.assertEqual(response.data[1]["month"], 1)

    def test_costs_filters_by_project(self):
        # Create items for different projects
        other_project = structure_factories.ProjectFactory()

        factories.InvoiceItemFactory(
            invoice=self.invoice, project=self.project, unit_price=100, quantity=1
        )
        factories.InvoiceItemFactory(
            invoice=self.invoice, project=other_project, unit_price=200, quantity=1
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + f"?project_uuid={self.project.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["price"], "100.00")

    def test_costs_includes_items_for_current_month(self):
        """Current month cost entry should include individual invoice items."""
        factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=Decimal("1.00"),
            quantity=Decimal("10"),
            unit=UnitPriceMixin.Units.PER_DAY,
            name="CPU-Hours / My CPU Service",
            measured_unit="CPU-Hours",
        )
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + f"?project_uuid={self.project.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        current = response.data[0]
        self.assertIn("items", current)
        self.assertEqual(len(current["items"]), 1)
        item = current["items"][0]
        self.assertEqual(item["name"], "CPU-Hours / My CPU Service")
        self.assertEqual(float(item["unit_price"]), 1.00)
        self.assertEqual(item["unit"], UnitPriceMixin.Units.PER_DAY)
        self.assertEqual(float(item["quantity"]), 10.0)
        self.assertEqual(item["measured_unit"], "CPU-Hours")

    def test_costs_excludes_items_for_past_months(self):
        """Past month cost entries should NOT include individual items."""
        old_invoice = factories.InvoiceFactory(year=2023, month=1)
        factories.InvoiceItemFactory(
            invoice=old_invoice, project=self.project, unit_price=100, quantity=1
        )
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + f"?project_uuid={self.project.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("items", response.data[0])

    def test_costs_excludes_zero_price_items(self):
        """Zero-price items should be excluded from the breakdown."""
        factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=Decimal("0"),
            quantity=10,
            name="Free item",
            measured_unit="units",
        )
        factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=Decimal("5.00"),
            quantity=1,
            name="Paid item",
            measured_unit="units",
        )
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url + f"?project_uuid={self.project.uuid.hex}")
        items = response.data[0].get("items", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Paid item")


class InvoiceItemDetailSerializerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_serializer_includes_offering_uuid_from_resource(self):
        """Test that offering_uuid is correctly serialized from resource.offering.uuid"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(self.fixture.invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that offering_uuid is present and matches the resource's offering UUID
        self.assertIn("offering_uuid", response.data)
        self.assertEqual(
            str(response.data["offering_uuid"]),
            str(self.fixture.resource.offering.uuid),
        )

    def test_serializer_includes_offering_component_type_from_direct_relationship(self):
        """Test offering_component_type from direct plan_component relationship"""
        # Create an invoice item with direct plan_component relationship
        plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.fixture.plan,
            component__type="cpu",
            component__billing_type=BillingTypes.FIXED,
        )

        invoice_item = factories.InvoiceItemFactory(
            resource=self.fixture.resource,
            plan_component=plan_component,
            invoice=self.fixture.invoice,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that offering_component_type is present and correct
        self.assertIn("offering_component_type", response.data)
        self.assertEqual(response.data["offering_component_type"], "cpu")

    def test_serializer_fallback_to_details_field_for_offering_component_type(self):
        """Test fallback to details field for backward compatibility"""
        # Create an invoice item without direct plan_component but with details
        invoice_item = factories.InvoiceItemFactory(
            resource=self.fixture.resource,
            plan_component=None,  # No direct relationship
            invoice=self.fixture.invoice,
            details={"offering_component_type": "memory", "plan_component_id": 123},
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that offering_component_type falls back to details
        self.assertIn("offering_component_type", response.data)
        self.assertEqual(response.data["offering_component_type"], "memory")

    def test_serializer_returns_none_when_no_component_type_available(self):
        """Test that None is returned when no component type is available"""
        # Create an invoice item without plan_component and without details
        invoice_item = factories.InvoiceItemFactory(
            resource=self.fixture.resource,
            plan_component=None,
            invoice=self.fixture.invoice,
            details={},  # No offering_component_type in details
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that offering_component_type is None
        self.assertIn("offering_component_type", response.data)
        self.assertIsNone(response.data["offering_component_type"])

    def test_direct_relationship_takes_precedence_over_details(self):
        """Test that direct plan_component relationship takes precedence over details"""
        plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.fixture.plan,
            component__type="storage",
            component__billing_type=BillingTypes.USAGE,
        )

        # Create item with both direct relationship and details (different values)
        invoice_item = factories.InvoiceItemFactory(
            resource=self.fixture.resource,
            plan_component=plan_component,
            invoice=self.fixture.invoice,
            details={"offering_component_type": "cpu"},  # Different from plan_component
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should use direct relationship, not details
        self.assertEqual(response.data["offering_component_type"], "storage")

    def test_serializer_includes_offering_name_from_resource(self):
        """Test that offering_name is correctly serialized from resource.offering.name"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(self.fixture.invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("offering_name", response.data)
        self.assertEqual(
            response.data["offering_name"],
            self.fixture.resource.offering.name,
        )

    def test_serializer_fallback_to_details_field_for_offering_name(self):
        """Test fallback to details field when resource is missing"""
        invoice_item = factories.InvoiceItemFactory(
            resource=None,
            invoice=self.fixture.invoice,
            details={"offering_name": "Test Offering"},
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("offering_name", response.data)
        self.assertEqual(response.data["offering_name"], "Test Offering")

    def test_serializer_returns_none_when_no_offering_name_available(self):
        """Test that None is returned when no offering name is available"""
        invoice_item = factories.InvoiceItemFactory(
            resource=None,
            invoice=self.fixture.invoice,
            details={},
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.InvoiceItemFactory.get_url(invoice_item)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("offering_name", response.data)
        self.assertIsNone(response.data["offering_name"])


class InvoiceItemModelTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_get_plan_component_uses_direct_relationship_first(self):
        """Test that get_plan_component() uses direct relationship when available"""
        plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.fixture.plan, component__type="cpu"
        )

        invoice_item = factories.InvoiceItemFactory(
            plan_component=plan_component,
            details={"plan_component_id": 999},  # Different ID to ensure direct is used
        )

        result = invoice_item.get_plan_component()
        self.assertEqual(result, plan_component)

    def test_get_plan_component_fallback_to_details(self):
        """Test fallback to details field when no direct relationship"""
        plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.fixture.plan, component__type="memory"
        )

        invoice_item = factories.InvoiceItemFactory(
            plan_component=None, details={"plan_component_id": plan_component.id}
        )

        result = invoice_item.get_plan_component()
        self.assertEqual(result, plan_component)

    def test_get_plan_component_returns_none_when_not_found(self):
        """Test that None is returned when plan component is not found"""
        invoice_item = factories.InvoiceItemFactory(
            plan_component=None,
            details={"plan_component_id": 99999},  # Non-existent ID
        )

        result = invoice_item.get_plan_component()
        self.assertIsNone(result)

    def test_get_plan_component_returns_none_when_no_data(self):
        """Test that None is returned when no plan component data available"""
        invoice_item = factories.InvoiceItemFactory(plan_component=None, details={})

        result = invoice_item.get_plan_component()
        self.assertIsNone(result)


class InvoiceItemProjectScopeVisibilityTest(test.APITestCase):
    """Project-scope roles see invoice items / costs of their project only
    when the owning customer displays billing info in projects."""

    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.item = self.fixture.invoice_item  # unit_price=10, quantity=30
        self.costs_url = factories.InvoiceItemFactory.get_list_url("costs")
        self.list_url = factories.InvoiceItemFactory.get_list_url()

    def get_costs(self, user, project=None):
        project = project or self.fixture.project
        self.client.force_authenticate(user)
        return self.client.get(self.costs_url, {"project_uuid": project.uuid.hex})

    def hide_billing_info(self):
        self.fixture.customer.display_billing_info_in_projects = False
        self.fixture.customer.save(update_fields=["display_billing_info_in_projects"])

    def test_project_user_sees_costs_when_billing_info_is_displayed(self):
        for user in (self.fixture.admin, self.fixture.manager, self.fixture.member):
            response = self.get_costs(user)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), 1)
            self.assertEqual(response.data[0]["price"], "300.00")

    def test_project_user_does_not_see_costs_when_billing_info_is_hidden(self):
        self.hide_billing_info()
        response = self.get_costs(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_owner_sees_costs_regardless_of_billing_info_flag(self):
        self.hide_billing_info()
        response = self.get_costs(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["price"], "300.00")

    def test_project_user_does_not_see_costs_of_other_project(self):
        other_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=other_project,
            unit_price=5,
            quantity=1,
        )
        response = self.get_costs(self.fixture.admin, project=other_project)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_project_user_can_list_invoice_items_when_billing_info_is_displayed(self):
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.item.uuid.hex, [item["uuid"] for item in response.data])

    def test_project_user_can_not_list_invoice_items_when_billing_info_is_hidden(self):
        self.hide_billing_info()
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(self.item.uuid.hex, [item["uuid"] for item in response.data])
