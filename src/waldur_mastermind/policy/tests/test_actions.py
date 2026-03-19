from unittest import mock

from ddt import data, ddt
from django.db.models.signals import post_save
from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    BillingTypes,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import policy_actions, tasks
from waldur_mastermind.policy.tests import factories


@override_settings(task_always_eager=True)
@freeze_time("2024-09-01")
@ddt
class ActionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.project_policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, created_by=self.fixture.user
        )
        self.customer_policy = factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer, created_by=self.fixture.user
        )
        self.admin = self.fixture.admin
        self.owner = self.fixture.owner

        structure_factories.NotificationFactory(
            key="marketplace_policy.notification_about_project_cost_exceeded_limit"
        )
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            month=9,
            year=2024,
            tax_percent=0,
        )

    def create_invoice_item(self, unit_price):
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            quantity=1,
            unit_price=unit_price,
        )

    @mock.patch("waldur_core.core.utils.send_mail")
    def test_notify_project_team(self, mock_send_mail):
        self.project_policy.actions = "notify_project_team"
        self.project_policy.save()

        serialized_policy = core_utils.serialize_instance(self.project_policy)
        tasks.notify_project_team(serialized_policy)

        mock_send_mail.assert_called_once()

        self.assertTrue(
            logging_models.Event.objects.filter(event_type="policy_notification")
        )

    def test_block_first_resource_creation_with_zero_threshold_policy(self):
        """Test that first resource is blocked when cost exceeds policy limit."""
        # Setup: Create policy with limit_cost=20, actions='block_creation_of_new_resources'
        self.project_policy.limit_cost = 20
        self.project_policy.actions = "block_creation_of_new_resources"
        self.project_policy.save()

        # Create CustomerCredit with value=20
        invoices_factories.CustomerCreditFactory(
            customer=self.customer,
            value=20,
        )

        # Create offering with limit-based component
        offering = marketplace_factories.OfferingFactory(customer=self.customer)
        plan = marketplace_factories.PlanFactory(offering=offering, unit_price=0)
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.LIMIT,
            type="cpu",
        )
        marketplace_factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=10,
        )

        # Attempt to create Resource with cost=100 (10 CPUs * 10 price)
        project_url = structure_factories.ProjectFactory.get_url(self.project)
        offering_url = marketplace_factories.OfferingFactory.get_public_url(offering)
        plan_url = marketplace_factories.PlanFactory.get_public_url(plan)

        payload = {
            "project": project_url,
            "offering": offering_url,
            "plan": plan_url,
            "limits": {"cpu": 10},  # Cost = 10 * 10 = 100
            "attributes": {"name": "test_resource"},
        }
        self.client.force_login(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_list_url()
        response = self.client.post(url, payload)

        # Expected: Order created but resource in ERRED state due to PolicyException
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        resource = marketplace_models.Resource.objects.get(
            uuid=response.data["marketplace_resource_uuid"]
        )
        self.assertEqual(resource.state, ResourceStates.ERRED)
        self.assertIn("Policy is violated", resource.error_message)

    def test_allow_first_resource_creation_when_under_limit(self):
        """Test that first resource is allowed when cost is under policy limit."""
        # Setup: Create policy with limit_cost=150
        self.project_policy.limit_cost = 150
        self.project_policy.actions = "block_creation_of_new_resources"
        self.project_policy.save()

        # Create CustomerCredit with value=100
        invoices_factories.CustomerCreditFactory(
            customer=self.customer,
            value=100,
        )

        # Create offering with limit-based component
        offering = marketplace_factories.OfferingFactory(customer=self.customer)
        plan = marketplace_factories.PlanFactory(offering=offering, unit_price=0)
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.LIMIT,
            type="cpu",
        )
        marketplace_factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=10,
        )

        # Attempt to create Resource with cost=100 (10 CPUs * 10 price)
        # Projected cost - compensation = 100 - 100 = 0 < 150 (limit)
        project_url = structure_factories.ProjectFactory.get_url(self.project)
        offering_url = marketplace_factories.OfferingFactory.get_public_url(offering)
        plan_url = marketplace_factories.PlanFactory.get_public_url(plan)

        payload = {
            "project": project_url,
            "offering": offering_url,
            "plan": plan_url,
            "limits": {"cpu": 10},  # Cost = 10 * 10 = 100
            "attributes": {"name": "test_resource"},
        }
        self.client.force_login(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_list_url()
        response = self.client.post(url, payload)

        # Expected: Resource created successfully
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        resource = marketplace_models.Resource.objects.get(
            uuid=response.data["marketplace_resource_uuid"]
        )
        self.assertNotEqual(resource.state, ResourceStates.ERRED)

    def test_second_resource_still_blocked_by_existing_mechanism(self):
        """Test that second resource is blocked by existing signal-based mechanism."""
        # Setup: Create policy
        self.project_policy.limit_cost = 0
        self.project_policy.actions = "block_creation_of_new_resources"
        self.project_policy.save()

        # Create first resource (should be allowed since proactive check allows it with no existing invoices)
        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Create invoice item to trigger policy
        self.create_invoice_item(100)

        # Attempt to create second resource
        response = self.create_order()

        # Expected: Blocked by existing signal-based mechanism
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_multiple_policies_checked(self):
        """Test that all relevant policies are checked."""
        # Create two policies with different limits
        factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=200,
            actions="block_creation_of_new_resources",
        )
        factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=50,
            actions="block_creation_of_new_resources",
        )

        # Create offering with limit-based component
        offering = marketplace_factories.OfferingFactory(customer=self.customer)
        plan = marketplace_factories.PlanFactory(offering=offering, unit_price=0)
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.LIMIT,
            type="cpu",
        )
        marketplace_factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=10,
        )

        # Attempt to create Resource with cost=100
        # Should violate policy2 (limit=50) even though policy1 (limit=200) would allow it
        project_url = structure_factories.ProjectFactory.get_url(self.project)
        offering_url = marketplace_factories.OfferingFactory.get_public_url(offering)
        plan_url = marketplace_factories.PlanFactory.get_public_url(plan)

        payload = {
            "project": project_url,
            "offering": offering_url,
            "plan": plan_url,
            "limits": {"cpu": 10},  # Cost = 100
            "attributes": {"name": "test_resource"},
        }
        self.client.force_login(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_list_url()
        response = self.client.post(url, payload)

        # Expected: Blocked by policy2
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        resource = marketplace_models.Resource.objects.get(
            uuid=response.data["marketplace_resource_uuid"]
        )
        self.assertEqual(resource.state, ResourceStates.ERRED)
        self.assertIn("Policy is violated", resource.error_message)

    @mock.patch("waldur_core.core.utils.send_mail")
    def test_notify_organization_owners(self, mock_send_mail):
        self.customer_policy.actions = "notify_organization_owners"
        self.customer_policy.save()

        serialized_policy = core_utils.serialize_instance(self.customer_policy)
        tasks.notify_customer_owners(serialized_policy)

        mock_send_mail.assert_called_once()
        self.assertEqual(mock_send_mail.call_args.kwargs["to"][0], self.owner.email)

        self.assertTrue(
            logging_models.Event.objects.filter(event_type="policy_notification")
        )

    @mock.patch("waldur_mastermind.policy.policy_actions.tasks")
    def test_create_event_log(self, mock_tasks):
        self.customer_policy.actions = "notify_organization_owners"
        self.customer_policy.save()
        self.create_invoice_item(self.customer_policy.limit_cost + 1)

        mock_tasks.notify_customer_owners.delay.assert_called_once()
        self.assertTrue(
            logging_models.Event.objects.filter(event_type="notify_organization_owners")
        )

    def create_order(self):
        project_url = structure_factories.ProjectFactory.get_url(self.fixture.project)
        offering_url = marketplace_factories.OfferingFactory.get_public_url(
            self.fixture.offering
        )
        plan_url = marketplace_factories.PlanFactory.get_public_url(self.fixture.plan)

        payload = {
            "project": project_url,
            "offering": offering_url,
            "plan": plan_url,
            "attributes": {"name": "item_name", "description": "Description"},
        }
        self.client.force_login(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_list_url()
        return self.client.post(url, payload)

    @data("customer_policy", "project_policy")
    def test_block_creation_of_new_resources(self, policy_name):
        policy = getattr(self, policy_name)
        policy.actions = "block_creation_of_new_resources"
        policy.save()

        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.create_invoice_item(policy.limit_cost + 1)

        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("customer_policy", "project_policy")
    def test_block_modification_of_existing_resources(self, policy_name):
        policy = getattr(self, policy_name)
        policy.actions = "block_modification_of_existing_resources"
        policy.save()

        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        resource = marketplace_models.Resource.objects.get(
            uuid=response.data["marketplace_resource_uuid"]
        )

        resource.set_state_ok()
        resource.save()
        self.create_invoice_item(policy.limit_cost + 1)

        self.client.force_authenticate(self.fixture.staff)
        url = marketplace_factories.ResourceFactory.get_url(resource, "update_limits")
        payload = {"limits": {"cpu": 2}}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("customer_policy", "project_policy")
    def test_terminate_resources(self, policy_name):
        policy = getattr(self, policy_name)
        policy.actions = "terminate_resources"
        policy.save()

        resource = self.fixture.resource
        resource.state = ResourceStates.OK
        resource.save()

        resource.offering.type = OPENSTACK_INSTANCE_OFFERING
        resource.offering.save()

        self.create_invoice_item(self.project_policy.limit_cost + 1)

        self.assertTrue(
            marketplace_models.Order.objects.filter(
                resource=resource,
                type=OrderTypes.TERMINATE,
            ).exists()
        )
        order = marketplace_models.Order.objects.filter(
            resource=resource,
            type=OrderTypes.TERMINATE,
        ).get()
        self.assertEqual(order.attributes, {"action": "force_destroy"})

    @data("customer_policy", "project_policy")
    def test_request_downscaling(self, policy_name):
        policy = getattr(self, policy_name)
        policy.actions = "request_downscaling"
        policy.save()

        resource = self.fixture.resource
        offering = resource.offering
        offering.plugin_options.update({"supports_downscaling": True})
        offering.save()

        self.create_invoice_item(policy.limit_cost + 1)

        resource.refresh_from_db()
        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)
        self.assertTrue(resource.downscaled)

    @data("customer_policy", "project_policy")
    def test_request_pausing(self, policy_name):
        policy = getattr(self, policy_name)
        policy.actions = "request_pausing"
        policy.save()

        resource = self.fixture.resource
        offering = resource.offering
        offering.plugin_options.update({"supports_pausing": True})
        offering.save()

        self.create_invoice_item(policy.limit_cost + 1)

        resource.refresh_from_db()
        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)
        self.assertTrue(resource.paused)

    @data("customer_policy", "project_policy")
    def test_restrict_members(self, policy_name):
        policy = getattr(self, policy_name)
        policy.actions = "restrict_members"
        policy.created_by = self.fixture.user
        policy.save()

        resource = self.fixture.resource
        offering = resource.offering
        offering.plugin_options["service_provider_can_create_offering_user"] = True
        offering.save()

        self.create_invoice_item(policy.limit_cost + 1)

        resource.refresh_from_db()
        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)
        self.assertTrue(resource.restrict_member_access)

    @data("customer_policy", "project_policy")
    @mock.patch("waldur_core.core.utils.send_mail")
    def test_notify_external_user(self, policy_name, mock_send_mail):
        policy = getattr(self, policy_name)
        external_user_email = "external_user@domen.com"
        policy.actions = "notify_external_user"
        policy.options = {"notify_external_user": external_user_email}
        policy.save()

        serialized_policy = core_utils.serialize_instance(policy)
        tasks.notify_external_user(serialized_policy)

        mock_send_mail.assert_called_once()
        self.assertEqual(mock_send_mail.call_args.kwargs["to"][0], external_user_email)

        self.assertTrue(
            logging_models.Event.objects.filter(event_type="policy_notification")
        )


@freeze_time("2024-09-01")
class PolicyActionsPostSaveSignalTest(test.APITestCase):
    """Test that policy actions use .save() so that post_save signals fire.

    This is a regression test for a bug where .update() was used instead of
    .save(), causing Django post_save signals to be bypassed. Without signals,
    the site agent handler never fires and STOMP notifications are not sent.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, created_by=self.fixture.user
        )

    def _setup_offering(self, **plugin_options):
        offering = self.resource.offering
        offering.plugin_options.update(plugin_options)
        offering.save()

    def _connect_signal(self):
        handler = mock.MagicMock()
        post_save.connect(handler, sender=marketplace_models.Resource)
        self.addCleanup(
            post_save.disconnect, handler, sender=marketplace_models.Resource
        )
        return handler

    def test_request_downscaling_triggers_post_save(self):
        self._setup_offering(supports_downscaling=True)
        handler = self._connect_signal()

        policy_actions.request_downscaling(self.policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        handler.assert_called()
        saved_instance = handler.call_args[1]["instance"]
        self.assertTrue(saved_instance.downscaled)

    def test_reset_downscaling_triggers_post_save(self):
        self._setup_offering(supports_downscaling=True)
        self.resource.downscaled = True
        self.resource.save()

        handler = self._connect_signal()

        policy_actions.reset_downscaling(self.policy)

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.downscaled)
        handler.assert_called()
        saved_instance = handler.call_args[1]["instance"]
        self.assertFalse(saved_instance.downscaled)

    def test_request_pausing_triggers_post_save(self):
        self._setup_offering(supports_pausing=True)
        handler = self._connect_signal()

        policy_actions.request_pausing(self.policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        handler.assert_called()
        saved_instance = handler.call_args[1]["instance"]
        self.assertTrue(saved_instance.paused)

    def test_reset_pausing_triggers_post_save(self):
        self._setup_offering(supports_pausing=True)
        self.resource.paused = True
        self.resource.save()

        handler = self._connect_signal()

        policy_actions.reset_pausing(self.policy)

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        handler.assert_called()
        saved_instance = handler.call_args[1]["instance"]
        self.assertFalse(saved_instance.paused)

    def test_restrict_members_triggers_post_save(self):
        self._setup_offering(service_provider_can_create_offering_user=True)
        handler = self._connect_signal()

        policy_actions.restrict_members(self.policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.restrict_member_access)
        handler.assert_called()
        saved_instance = handler.call_args[1]["instance"]
        self.assertTrue(saved_instance.restrict_member_access)

    def test_reset_member_restriction_triggers_post_save(self):
        self._setup_offering(service_provider_can_create_offering_user=True)
        self.resource.restrict_member_access = True
        self.resource.save()

        handler = self._connect_signal()

        policy_actions.reset_member_restriction(self.policy)

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.restrict_member_access)
        handler.assert_called()
        saved_instance = handler.call_args[1]["instance"]
        self.assertFalse(saved_instance.restrict_member_access)

    def test_request_downscaling_skips_noop_save(self):
        """When resource is already downscaled, no save should occur."""
        self._setup_offering(supports_downscaling=True)
        self.resource.downscaled = True
        self.resource.save()

        handler = self._connect_signal()
        policy_actions.request_downscaling(self.policy)
        handler.assert_not_called()

    def test_reset_downscaling_skips_noop_save(self):
        """When resource is already not downscaled, no save should occur."""
        self._setup_offering(supports_downscaling=True)
        handler = self._connect_signal()
        policy_actions.reset_downscaling(self.policy)
        handler.assert_not_called()

    def test_request_pausing_skips_noop_save(self):
        """When resource is already paused, no save should occur."""
        self._setup_offering(supports_pausing=True)
        self.resource.paused = True
        self.resource.save()

        handler = self._connect_signal()
        policy_actions.request_pausing(self.policy)
        handler.assert_not_called()

    def test_reset_pausing_skips_noop_save(self):
        """When resource is already not paused, no save should occur."""
        self._setup_offering(supports_pausing=True)
        handler = self._connect_signal()
        policy_actions.reset_pausing(self.policy)
        handler.assert_not_called()

    def test_restrict_members_skips_noop_save(self):
        """When resource already has restricted access, no save should occur."""
        self._setup_offering(service_provider_can_create_offering_user=True)
        self.resource.restrict_member_access = True
        self.resource.save()

        handler = self._connect_signal()
        policy_actions.restrict_members(self.policy)
        handler.assert_not_called()

    def test_reset_member_restriction_skips_noop_save(self):
        """When resource already has unrestricted access, no save should occur."""
        self._setup_offering(service_provider_can_create_offering_user=True)
        handler = self._connect_signal()
        policy_actions.reset_member_restriction(self.policy)
        handler.assert_not_called()
