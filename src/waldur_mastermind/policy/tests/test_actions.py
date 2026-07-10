import threading
from unittest import mock

from ddt import data, ddt
from django.db.models.signals import post_save
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
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
from waldur_mastermind.policy import policy_actions, tasks, utils
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
        order_count_before = marketplace_models.Order.objects.count()
        resource_count_before = marketplace_models.Resource.objects.count()
        self.client.force_login(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_list_url()
        response = self.client.post(url, payload)

        # Expected: order rejected at API boundary; no resource is persisted [HPCMP-484].
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(marketplace_models.Order.objects.count(), order_count_before)
        self.assertEqual(
            marketplace_models.Resource.objects.count(), resource_count_before
        )

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order")
    def test_allow_first_resource_creation_when_under_limit(self, mock_process_order):
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

        # Expected: blocked by policy2 at the API boundary [HPCMP-484].
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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

    def test_request_pausing_event_carries_resource_identity(self):
        """The pausing event must carry the resource name and uuid so the
        activity log renders "<resource> has been paused by a cost policy"
        with a working link instead of a blank prefix."""
        policy = self.project_policy
        policy.actions = "request_pausing"
        policy.save()

        resource = self.fixture.resource
        offering = resource.offering
        offering.plugin_options.update({"supports_pausing": True})
        offering.save()

        self.create_invoice_item(policy.limit_cost + 1)

        event = logging_models.Event.objects.filter(
            event_type="request_pausing"
        ).latest("created")
        self.assertEqual(event.context.get("resource_uuid"), resource.uuid.hex)
        self.assertEqual(event.context.get("resource_name"), resource.name)

    def test_pausing_event_attributed_to_system_robot_not_request_user(self):
        """When a policy fires inside a user's request context, the emitted
        event must be attributed to the system robot, not the ambient request
        user (whose save happened to trigger the evaluation)."""
        from waldur_core.core.utils import get_system_robot
        from waldur_core.logging import middleware as logging_middleware

        policy = self.project_policy
        policy.actions = "request_pausing"
        policy.save()

        resource = self.fixture.resource
        offering = resource.offering
        offering.plugin_options.update({"supports_pausing": True})
        offering.save()

        robot = get_system_robot()
        # Simulate an authenticated request context, as
        # CaptureEventContextMiddleware would set during an HTTP request.
        logging_middleware.set_event_context(
            {
                "user_username": "requesting_user",
                "user_uuid": "0" * 32,
                "ip_address": "203.0.113.7",
            }
        )
        try:
            self.create_invoice_item(policy.limit_cost + 1)
        finally:
            logging_middleware.reset_event_context()

        event = logging_models.Event.objects.filter(
            event_type="request_pausing"
        ).latest("created")
        self.assertEqual(event.context.get("user_username"), robot.username)
        self.assertNotEqual(event.context.get("user_username"), "requesting_user")
        self.assertNotIn("ip_address", event.context)

    def test_policy_events_on_async_path_skip_system_robot_lookup(self):
        """On the background path (no request context) there is no request user
        to hide, so the event carries no user and the system-robot lookup is
        skipped entirely — keeping burst policy evaluation off the User table."""
        from waldur_core.logging import middleware as logging_middleware

        policy = self.project_policy
        policy.actions = "request_pausing"
        policy.save()
        resource = self.fixture.resource
        offering = resource.offering
        offering.plugin_options.update({"supports_pausing": True})
        offering.save()

        logging_middleware.reset_event_context()  # no ambient request context
        with mock.patch("waldur_mastermind.policy.utils.get_system_robot") as robot:
            self.create_invoice_item(policy.limit_cost + 1)
            robot.assert_not_called()

        event = logging_models.Event.objects.filter(
            event_type="request_pausing"
        ).latest("created")
        self.assertNotIn("user_uuid", event.context)

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


@freeze_time("2024-09-01")
class PolicyActionReversionTest(test.APITestCase):
    """Test that resource-modifying policy actions create reversion entries."""

    def setUp(self):
        import reversion

        self.reversion = reversion
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

    def _get_versions(self):
        from reversion.models import Version

        return Version.objects.get_for_object(self.resource)

    def test_request_downscaling_creates_revision(self):
        self._setup_offering(supports_downscaling=True)
        policy_actions.request_downscaling(self.policy)

        versions = self._get_versions()
        self.assertGreaterEqual(versions.count(), 1)
        latest = versions.first()
        self.assertIn(self.policy.uuid.hex, latest.revision.comment)
        self.assertIn("request_downscaling", latest.revision.comment)
        self.assertEqual(latest.revision.user.username, "system_robot")

    def test_reset_downscaling_creates_revision(self):
        self._setup_offering(supports_downscaling=True)
        self.resource.downscaled = True
        self.resource.save()

        policy_actions.reset_downscaling(self.policy)

        versions = self._get_versions()
        self.assertGreaterEqual(versions.count(), 1)
        latest = versions.first()
        self.assertIn("reset_downscaling", latest.revision.comment)
        self.assertEqual(latest.revision.user.username, "system_robot")

    def test_request_pausing_creates_revision(self):
        self._setup_offering(supports_pausing=True)
        policy_actions.request_pausing(self.policy)

        versions = self._get_versions()
        self.assertGreaterEqual(versions.count(), 1)
        latest = versions.first()
        self.assertIn("request_pausing", latest.revision.comment)
        self.assertIn(self.policy.uuid.hex, latest.revision.comment)

    def test_reset_pausing_creates_revision(self):
        self._setup_offering(supports_pausing=True)
        self.resource.paused = True
        self.resource.save()

        policy_actions.reset_pausing(self.policy)

        versions = self._get_versions()
        self.assertGreaterEqual(versions.count(), 1)
        latest = versions.first()
        self.assertIn("reset_pausing", latest.revision.comment)

    def test_restrict_members_creates_revision(self):
        self._setup_offering(service_provider_can_create_offering_user=True)
        policy_actions.restrict_members(self.policy)

        versions = self._get_versions()
        self.assertGreaterEqual(versions.count(), 1)
        latest = versions.first()
        self.assertIn("restrict_members", latest.revision.comment)
        self.assertIn(self.policy.uuid.hex, latest.revision.comment)

    def test_reset_member_restriction_creates_revision(self):
        self._setup_offering(service_provider_can_create_offering_user=True)
        self.resource.restrict_member_access = True
        self.resource.save()

        policy_actions.reset_member_restriction(self.policy)

        versions = self._get_versions()
        self.assertGreaterEqual(versions.count(), 1)
        latest = versions.first()
        self.assertIn("reset_member_restriction", latest.revision.comment)

    def test_policy_attribution_stored_in_attributes(self):
        """Verify _policy_attribution metadata is stored on the resource."""
        self._setup_offering(supports_pausing=True)
        policy_actions.request_pausing(self.policy)

        self.resource.refresh_from_db()
        attribution = self.resource.attributes.get("_policy_attribution", {})
        self.assertIn("paused", attribution)
        self.assertEqual(attribution["paused"]["policy_uuid"], self.policy.uuid.hex)
        self.assertEqual(attribution["paused"]["action"], "request_pausing")
        self.assertEqual(
            attribution["paused"]["policy_class"], "ProjectEstimatedCostPolicy"
        )


@freeze_time("2024-09-01")
class PolicyActionEventScopesTest(test.APITestCase):
    """Test that policy action events are scoped to the correct resources/projects/customers."""

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

    def _get_feeds_for_event_type(self, event_type_value):
        events = logging_models.Event.objects.filter(event_type=event_type_value)
        if not events.exists():
            return logging_models.Feed.objects.none()
        return logging_models.Feed.objects.filter(event__in=events)

    def test_request_downscaling_event_scoped_to_resource(self):
        self._setup_offering(supports_downscaling=True)
        policy_actions.request_downscaling(self.policy)

        feeds = self._get_feeds_for_event_type("request_downscaling")
        self.assertTrue(feeds.exists())
        scope_ids = set(feeds.values_list("object_id", flat=True))
        self.assertIn(self.resource.id, scope_ids)
        self.assertIn(self.project.id, scope_ids)

    def test_reset_downscaling_event_scoped_to_resource(self):
        self._setup_offering(supports_downscaling=True)
        self.resource.downscaled = True
        self.resource.save()

        policy_actions.reset_downscaling(self.policy)

        feeds = self._get_feeds_for_event_type("reset_downscaling")
        self.assertTrue(feeds.exists())
        scope_ids = set(feeds.values_list("object_id", flat=True))
        self.assertIn(self.resource.id, scope_ids)

    def test_request_pausing_event_uses_correct_type(self):
        """Verify request_pausing uses REQUEST_PAUSING, not BLOCK_MODIFICATION."""
        self._setup_offering(supports_pausing=True)
        policy_actions.request_pausing(self.policy)

        events = logging_models.Event.objects.filter(event_type="request_pausing")
        self.assertTrue(events.exists())
        # Ensure the old incorrect type is not used
        wrong_events = logging_models.Event.objects.filter(
            event_type="block_modification_of_existing_resources",
            message__icontains="pausing",
        )
        self.assertFalse(wrong_events.exists())

    def test_restrict_members_event_scoped_to_resource(self):
        self._setup_offering(service_provider_can_create_offering_user=True)
        policy_actions.restrict_members(self.policy)

        feeds = self._get_feeds_for_event_type("restrict_members")
        self.assertTrue(feeds.exists())
        scope_ids = set(feeds.values_list("object_id", flat=True))
        self.assertIn(self.resource.id, scope_ids)

    def test_notify_project_team_event_scoped_to_project(self):
        policy_actions.notify_project_team(self.policy)

        feeds = self._get_feeds_for_event_type("notify_project_team")
        self.assertTrue(feeds.exists())
        scope_ids = set(feeds.values_list("object_id", flat=True))
        self.assertIn(self.project.id, scope_ids)
        self.assertIn(self.customer.id, scope_ids)


@freeze_time("2024-09-01")
class PolicyActionBulkPerformanceTest(test.APITestCase):
    """Test that bulk optimisations work correctly with many resources."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.offering = self.fixture.resource.offering
        self.offering.plugin_options.update(
            {
                "supports_downscaling": True,
                "supports_pausing": True,
                "service_provider_can_create_offering_user": True,
            }
        )
        self.offering.save()
        self.policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, created_by=self.fixture.user
        )

        # Create multiple resources in the same project/offering
        self.resources = [self.fixture.resource]
        for i in range(4):
            resource = marketplace_factories.ResourceFactory(
                project=self.project,
                offering=self.offering,
                state=ResourceStates.OK,
            )
            self.resources.append(resource)

    def test_bulk_downscaling_creates_events_for_all_resources(self):
        policy_actions.request_downscaling(self.policy)

        events = logging_models.Event.objects.filter(event_type="request_downscaling")
        self.assertEqual(events.count(), 5)

        for resource in self.resources:
            resource.refresh_from_db()
            self.assertTrue(resource.downscaled)

        feeds = logging_models.Feed.objects.filter(event__in=events)
        # Each resource gets 3 feeds: resource + project + customer
        self.assertEqual(feeds.count(), 5 * 3)

    def test_bulk_downscaling_creates_revisions_for_all_resources(self):
        from reversion.models import Version

        policy_actions.request_downscaling(self.policy)

        for resource in self.resources:
            versions = Version.objects.get_for_object(resource)
            self.assertGreaterEqual(versions.count(), 1)
            self.assertIn("request_downscaling", versions.first().revision.comment)

    def test_bulk_pausing_creates_events_for_all_resources(self):
        policy_actions.request_pausing(self.policy)

        events = logging_models.Event.objects.filter(event_type="request_pausing")
        self.assertEqual(events.count(), 5)

        for resource in self.resources:
            resource.refresh_from_db()
            self.assertTrue(resource.paused)

    def test_bulk_reset_only_affects_flagged_resources(self):
        """Reset should only create events for resources that actually change."""
        # Only downscale 2 of 5
        for resource in self.resources[:2]:
            resource.downscaled = True
            resource.save()

        policy_actions.reset_downscaling(self.policy)

        events = logging_models.Event.objects.filter(event_type="reset_downscaling")
        self.assertEqual(events.count(), 2)

        for resource in self.resources:
            resource.refresh_from_db()
            self.assertFalse(resource.downscaled)

    def test_bulk_action_stores_attribution_on_all_resources(self):
        policy_actions.restrict_members(self.policy)

        for resource in self.resources:
            resource.refresh_from_db()
            self.assertTrue(resource.restrict_member_access)
            attribution = resource.attributes.get("_policy_attribution", {})
            self.assertIn("restrict_member_access", attribution)
            self.assertEqual(
                attribution["restrict_member_access"]["policy_uuid"],
                self.policy.uuid.hex,
            )

    def test_noop_bulk_action_creates_no_events(self):
        """If all resources already have the target value, no events should be created."""
        for resource in self.resources:
            resource.paused = True
            resource.save()

        policy_actions.request_pausing(self.policy)

        events = logging_models.Event.objects.filter(event_type="request_pausing")
        self.assertEqual(events.count(), 0)


@freeze_time("2024-09-01")
class PolicyActionReentrantSignalTest(test.APITestCase):
    """Test that policy actions don't crash when another policy on the same
    project uses block_modification_of_existing_resources.

    Regression test: resource.save() inside a policy action triggers post_save,
    which re-evaluates all policies on the project. If a block_modification
    policy has already fired, it raises PolicyException, crashing the original
    action. The fix sets is_mocked on the resource to skip re-entrant evaluation.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.resource.offering.plugin_options["supports_pausing"] = True
        self.resource.offering.save()

        # Policy 1: the action we want to test
        self.pause_policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            created_by=self.fixture.user,
            actions="request_pausing",
        )

        # Policy 2: a block_modification policy that has already fired
        # (threshold actions run when has_fired=True on the post_save handler)
        self.block_policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            created_by=self.fixture.user,
            actions="block_modification_of_existing_resources",
            has_fired=True,
        )

    def test_request_pausing_succeeds_with_block_modification_policy(self):
        """request_pausing must not be blocked by a sibling block_modification policy."""
        # This would raise PolicyException without the is_mocked fix
        policy_actions.request_pausing(self.pause_policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    def test_downscaling_succeeds_with_block_modification_policy(self):
        self.resource.offering.plugin_options["supports_downscaling"] = True
        self.resource.offering.save()

        policy_actions.request_downscaling(self.pause_policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)


@override_settings(task_always_eager=True)
class ConcurrentPolicyEvaluationTest(TransactionTestCase):
    """WAL-9807: Verify that concurrent policy evaluations fire actions exactly once.

    Without atomic CAS on has_fired, two concurrent evaluate_policies() calls
    can both read has_fired=False, both fire actions, and both save has_fired=True.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture.project,
            created_by=self.fixture.user,
        )
        self.policy.actions = "request_pausing"
        self.policy.limit_cost = (
            0  # Any cost exceeds limit, so is_triggered() returns True
        )
        self.policy.save()

        self.resource = self.fixture.resource
        self.resource.offering.plugin_options = {"supports_pausing": True}
        self.resource.offering.save()
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()

        # Use existing invoice from fixture or get/create one
        from waldur_mastermind.invoices.models import Invoice

        now = timezone.now()
        invoice, _ = Invoice.objects.get_or_create(
            customer=self.fixture.customer,
            month=now.month,
            year=now.year,
            defaults={"tax_percent": 0},
        )
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=self.resource,
            project=self.fixture.project,
            unit_price=50,
            quantity=1,
        )

    def test_concurrent_evaluation_fires_actions_once(self):
        """Two concurrent evaluations must fire policy actions exactly once.

        With atomic CAS on has_fired, only one of two concurrent workers
        will successfully transition has_fired from False to True.
        We verify this by checking how many times the resource was paused.
        """
        barrier = threading.Barrier(2, timeout=10)
        errors = []

        def evaluate_after_barrier():
            try:
                barrier.wait()
                from waldur_mastermind.policy.models import (
                    ProjectEstimatedCostPolicy,
                )

                policies = ProjectEstimatedCostPolicy.objects.filter(pk=self.policy.pk)
                utils.evaluate_policies(policies)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=evaluate_after_barrier)
        t2 = threading.Thread(target=evaluate_after_barrier)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        self.assertEqual(errors, [], f"Threads raised errors: {errors}")

        self.policy.refresh_from_db()
        self.assertTrue(self.policy.has_fired)

        # Check that policy was only marked as fired once by counting
        # the reversion entries — each fire creates one revision.
        from reversion.models import Version

        versions = Version.objects.get_for_object(self.resource)
        pause_versions = [
            v for v in versions if "request_pausing" in (v.revision.comment or "")
        ]
        self.assertEqual(
            len(pause_versions),
            1,
            f"Resource was paused {len(pause_versions)} times, expected exactly 1. "
            "Double-fire indicates missing atomic CAS on has_fired.",
        )
