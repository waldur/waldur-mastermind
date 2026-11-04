from decimal import Decimal, InvalidOperation
from unittest.mock import MagicMock, patch

from ddt import data, ddt
from django.test import TestCase
from rest_framework import status, test

from waldur_core.core import access
from waldur_core.logging import models as logging_models
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import handlers, models, views
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.models import PlanComponent

from . import factories


@ddt
class PlanComponentsGetTest(test.APITestCase):
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
        # A project role does not reach the organization's private offering,
        # which neither public nor provider offerings show the admin.
        self.assertEqual(
            [self.shared_plan.name], [row["plan_name"] for row in response.data]
        )

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


@ddt
class PlanComponentsVisibilityTest(test.APITestCase):
    """Components of plans of offerings public or provider offerings show."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.shared_component = self.make_component(shared=True)
        self.private_component = self.make_component(shared=False)
        self.url = factories.PlanComponentFactory.get_list_url()

    def make_component(self, **offering_kwargs):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, **offering_kwargs
        )
        plan = factories.PlanFactory(offering=offering)
        return factories.PlanComponentFactory(
            plan=plan,
            component=factories.OfferingComponentFactory(offering=offering),
        )

    def list_ids(self, user=None):
        if user:
            self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        return sorted(str(row["plan_uuid"]) for row in response.data)

    def plan_ids(self, *components):
        return sorted(component.plan.uuid.hex for component in components)

    def test_plan_in_several_of_the_callers_groups_is_listed_once(self):
        groups = structure_factories.OrganizationGroupFactory.create_batch(2)
        self.shared_component.plan.organization_groups.add(*groups)
        consumer = structure_factories.CustomerFactory()
        consumer.organization_groups.add(*groups)
        owner = structure_factories.UserFactory()
        consumer.add_user(owner, CustomerRole.OWNER)

        self.assertEqual(self.list_ids(owner), self.plan_ids(self.shared_component))

    def retrieve(self, component, user=None):
        if user:
            self.client.force_authenticate(user)
        return self.client.get(f"{self.url}{component.pk}/")

    @data("user", "consumer_owner")
    def test_caller_without_provider_role_sees_only_public_offerings(self, name):
        user = (
            structure_factories.UserFactory()
            if name == "user"
            else fixtures.ProjectFixture().owner
        )
        self.assertEqual(self.plan_ids(self.shared_component), self.list_ids(user))
        self.assertEqual(
            status.HTTP_404_NOT_FOUND,
            self.retrieve(self.private_component).status_code,
        )
        self.assertEqual(
            status.HTTP_200_OK, self.retrieve(self.shared_component).status_code
        )

    def test_anonymous_caller_does_not_see_private_offering(self):
        self.assertEqual(self.plan_ids(self.shared_component), self.list_ids())
        self.assertEqual(
            status.HTTP_404_NOT_FOUND,
            self.retrieve(self.private_component).status_code,
        )

    def test_shared_offering_in_draft_is_hidden_from_consumers(self):
        draft = self.make_component(shared=True, state=OfferingStates.DRAFT)
        self.assertNotIn(
            draft.plan.uuid.hex, self.list_ids(structure_factories.UserFactory())
        )
        self.assertIn(draft.plan.uuid.hex, self.list_ids(self.fixture.owner))

    @data("staff", "global_support", "owner", "customer_support", "service_manager")
    def test_provider_side_users_see_private_offering(self, name):
        user = getattr(self.fixture, name)
        self.assertEqual(
            self.plan_ids(self.shared_component, self.private_component),
            self.list_ids(user),
        )
        self.assertEqual(
            status.HTTP_200_OK, self.retrieve(self.private_component).status_code
        )

    def test_offering_manager_sees_components_of_their_offering(self):
        manager = structure_factories.UserFactory()
        self.private_component.plan.offering.add_user(manager, OfferingRole.MANAGER)
        self.assertEqual(
            self.plan_ids(self.shared_component, self.private_component),
            self.list_ids(manager),
        )

    def test_routes_say_who_may_call_them(self):
        for action, detail in (("list", False), ("retrieve", True)):
            with self.subTest(action=action):
                view = views.PlanComponentViewSet(action=action, detail=detail)
                route = access.access_for(view, "GET")
                self.assertTrue(route.described)
                self.assertIn(views.PUBLIC_CATALOG_ACCESS, route.entries)
                self.assertEqual(access.CUSTOM_VISIBILITY, route.visibility)
                self.assertEqual(
                    views.PLAN_COMPONENT_VISIBILITY, route.visibility_description
                )


class PlanComponentUpdateLoggerTest(TestCase):
    def setUp(self):
        # Create mock objects
        self.instance = MagicMock()
        self.instance.component.type = "storage"
        self.instance.plan.name = "Premium Plan"

        # Configure tracker.has_changed to be more specific
        self.instance.tracker.has_changed = MagicMock(return_value=False)

        # The value the component held before the update.
        self.previous_value = Decimal("0.0010000000")

        # Original Decimal value
        self.decimal_value = Decimal("0.0020000000")

        # String representation of the same value
        self.string_value = "0.0020000000"

    @patch("waldur_core.logging.event_logger.emit")
    def test_update_with_decimal_value(self, mock_logger):
        """Test that a Decimal price value works correctly."""
        # Setup - only price has changed
        self.instance.price = self.decimal_value
        self.instance.tracker.previous.return_value = self.previous_value
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

    @patch("waldur_core.logging.event_logger.emit")
    def test_update_with_string_value(self, mock_logger):
        """Test that a string price value is correctly converted to Decimal."""
        # Setup - only price has changed
        self.instance.price = self.string_value
        self.instance.tracker.previous.return_value = self.previous_value
        self.instance.tracker.has_changed = lambda field: field == "price"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], self.decimal_value)
        self.assertIsInstance(event_context["new_value"], Decimal)

    @patch("waldur_core.logging.event_logger.emit")
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

    @patch("waldur_core.logging.event_logger.emit")
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

    @patch("waldur_core.logging.event_logger.emit")
    def test_update_with_future_price_string_value(self, mock_logger):
        """Test that a string future_price value is correctly converted to Decimal."""
        # Setup - only future_price has changed
        self.instance.future_price = self.string_value
        self.instance.tracker.previous.return_value = self.previous_value
        self.instance.tracker.has_changed = lambda field: field == "future_price"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], self.decimal_value)
        self.assertIsInstance(event_context["new_value"], Decimal)

    @patch("waldur_core.logging.event_logger.emit")
    def test_update_with_amount_string_value(self, mock_logger):
        """Test that a string amount value is correctly converted to Decimal."""
        # Setup - only amount has changed
        self.instance.amount = self.string_value
        self.instance.tracker.previous.return_value = self.previous_value
        self.instance.tracker.has_changed = lambda field: field == "amount"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_called_once()
        # Extract the event_context from the call
        event_context = mock_logger.call_args[1]["event_context"]
        self.assertEqual(event_context["new_value"], self.decimal_value)
        self.assertIsInstance(event_context["new_value"], Decimal)

    @patch("waldur_core.logging.event_logger.emit")
    def test_no_event_when_only_the_representation_of_the_price_changed(
        self, mock_logger
    ):
        """A price written back as a string is not a change worth logging."""
        # Setup - the tracker reports a change because "0.0020000000" is not
        # equal to Decimal("0.0020000000"), yet both mean the same price.
        self.instance.price = self.string_value
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "price"

        # Execute
        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        # Assert
        mock_logger.assert_not_called()

    @patch("waldur_core.logging.event_logger.emit")
    def test_no_event_when_only_trailing_zeros_of_the_price_changed(self, mock_logger):
        """Trailing zeros carry no price difference."""
        self.instance.price = "0.002"
        self.instance.tracker.previous.return_value = self.decimal_value
        self.instance.tracker.has_changed = lambda field: field == "price"

        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        mock_logger.assert_not_called()

    @patch("waldur_core.logging.event_logger.emit")
    def test_no_event_when_only_the_representation_of_the_amount_changed(
        self, mock_logger
    ):
        self.instance.amount = "10"
        self.instance.tracker.previous.return_value = 10
        self.instance.tracker.has_changed = lambda field: field == "amount"

        handlers.plan_component_has_been_updated(PlanComponent, self.instance)

        mock_logger.assert_not_called()

    def test_resaving_the_same_price_as_a_string_logs_nothing(self):
        """The shape an offering sync writes back must not produce an event."""
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)
        component = factories.OfferingComponentFactory(offering=offering)
        factories.PlanComponentFactory(plan=plan, component=component, price="0.0106")
        # Reload so that the tracker starts from the stored Decimal, the way
        # a sync that re-reads the component before writing it back does.
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=component
        )
        self.assertEqual(plan_component.price, Decimal("0.0106000000"))

        plan_component.price = "0.0106"
        plan_component.save(update_fields=["price"])

        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type="marketplace_plan_component_current_price_updated"
            ).exists()
        )
