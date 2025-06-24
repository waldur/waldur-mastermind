from decimal import Decimal, InvalidOperation
from unittest.mock import MagicMock, patch

from ddt import ddt
from django.test import TestCase
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import handlers
from waldur_mastermind.marketplace.models import PlanComponent

from . import factories


@ddt
class PlanComponentsGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.shared_offering = factories.OfferingFactory(
            customer=self.customer,
            shared=True,
        )
        self.shared_plan = factories.PlanFactory(offering=self.shared_offering)
        self.shared_offering_component = factories.OfferingComponentFactory(
            offering=self.shared_offering
        )
        self.shared_plan_component = factories.PlanComponentFactory(
            plan=self.shared_plan, component=self.shared_offering_component
        )

        self.private_offering = factories.OfferingFactory(
            customer=self.customer,
            shared=False,
        )
        self.private_plan = factories.PlanFactory(offering=self.private_offering)
        self.private_offering_component = factories.OfferingComponentFactory(
            offering=self.private_offering
        )
        self.private_plan_component = factories.PlanComponentFactory(
            plan=self.private_plan, component=self.private_offering_component
        )

        self.url = factories.PlanComponentFactory.get_list_url()

    def test_user_is_staff_and_plans_are_not_matched_with_organization_groups(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_not_staff_and_plans_are_not_matched_with_organization_groups(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_staff_and_plans_are_matched_with_organization_groups(self):
        organization_group = structure_factories.OrganizationGroupFactory()
        self.shared_plan.organization_groups.add(organization_group)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_not_staff_and_plans_are_matched_with_organization_groups(self):
        organization_group = structure_factories.OrganizationGroupFactory()
        self.shared_plan.organization_groups.add(organization_group)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_user_is_owner_and_plan_and_customer_are_connected_the_same_organization_group(
        self,
    ):
        organization_group = structure_factories.OrganizationGroupFactory()
        self.shared_plan.organization_groups.add(organization_group)
        self.customer.organization_groups.add(organization_group)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_admin_and_plan_and_project_customer_are_connected_the_same_organization_group(
        self,
    ):
        organization_group = structure_factories.OrganizationGroupFactory()
        self.shared_plan.organization_groups.add(organization_group)
        self.customer.organization_groups.add(organization_group)
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_getting_plan_components_by_unauthorized_user(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

        self.shared_plan.organization_groups.add(
            structure_factories.OrganizationGroupFactory()
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_filter_by_shared(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"shared": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]["plan_name"], self.shared_plan.name)

    def test_filter_by_archived(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        self.shared_plan.archived = True
        self.shared_plan.save()
        response = self.client.get(self.url, {"archived": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]["plan_name"], self.shared_plan.name)


class PlanComponentUpdateLoggerTest(TestCase):
    def setUp(self):
        # Create mock objects
        self.instance = MagicMock()
        self.instance.component.type = "storage"
        self.instance.plan.name = "Premium Plan"

        # Configure tracker.has_changed to be more specific
        self.instance.tracker.has_changed = MagicMock(return_value=False)

        # Original Decimal value
        self.decimal_value = Decimal("0.0020000000")

        # String representation of the same value
        self.string_value = "0.0020000000"

    @patch("waldur_core.core.log.event_logger.info")
    def test_update_with_decimal_value(self, mock_logger):
        """Test that a Decimal price value works correctly."""
        # Setup - only price has changed
        self.instance.price = self.decimal_value
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "price"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], self.decimal_value)
        self.assertIsInstance(event_context["new_value"], Decimal)

    def test_decimal_in_event_context(self):
        """Test that Decimal values are properly converted to string in Event's JSON context field"""
        new_decimal_value = Decimal("0.20000000")
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)
        component = factories.OfferingComponentFactory(offering=offering)
        plan_component = factories.PlanComponentFactory(
            plan=plan, component=component, price="1.00000000"
        )

        plan_component.price = new_decimal_value
        plan_component.save(update_fields=["price"])

        event = logging_models.Event.objects.get(
            event_type="marketplace_plan_component_current_price_updated"
        )
        self.assertEqual(event.context["new_value"], str(new_decimal_value))
        self.assertIsInstance(event.context["new_value"], str)

    @patch("waldur_core.core.log.event_logger.info")
    def test_update_with_string_value(self, mock_logger):
        """Test that a string price value is correctly converted to Decimal."""
        # Setup - only price has changed
        self.instance.price = self.string_value
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "price"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], self.decimal_value)
        self.assertIsInstance(event_context["new_value"], Decimal)

    @patch("waldur_core.core.log.event_logger.info")
    def test_update_with_invalid_string_value(self, mock_logger):
        """Test that an invalid string value raises the appropriate exception."""
        # Setup - only price has changed
        self.instance.price = "not-a-number"
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "price"

        # Execute and Assert
        with self.assertRaises(InvalidOperation):
            handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Verify logger wasn't called
        mock_logger.assert_not_called()

    @patch("waldur_core.core.log.event_logger.info")
    def test_update_with_none_value(self, mock_logger):
        """Test that a None value is handled appropriately."""
        # Setup - only price has changed
        self.instance.price = None
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "price"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], None)

    @patch("waldur_core.core.log.event_logger.info")
    def test_update_with_future_price_string_value(self, mock_logger):
        """Test that a string future_price value is correctly converted to Decimal."""
        # Setup - only future_price has changed
        self.instance.future_price = self.string_value
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "future_price"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], self.decimal_value)
        self.assertIsInstance(event_context["new_value"], Decimal)

    @patch("waldur_core.core.log.event_logger.info")
    def test_update_with_amount_string_value(self, mock_logger):
        """Test that a string amount value is correctly converted to Decimal."""
        # Setup - only amount has changed
        self.instance.amount = self.string_value
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "amount"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], self.decimal_value)
        self.assertIsInstance(event_context["new_value"], Decimal)
