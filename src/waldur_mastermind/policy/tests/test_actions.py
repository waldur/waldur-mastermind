from unittest import mock

from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE
from waldur_mastermind.policy import tasks
from waldur_mastermind.policy.tests import factories


class ActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(
            project=self.project, created_by=self.fixture.user
        )
        self.estimate = billing_models.PriceEstimate.objects.get(scope=self.project)
        self.admin = self.fixture.admin
        self.owner = self.fixture.owner

        structure_factories.NotificationFactory(
            key="marketplace_policy.notification_about_project_cost_exceeded_limit"
        )

    @mock.patch("waldur_core.core.utils.send_mail")
    def test_notify_project_team(self, mock_send_mail):
        self.policy.actions = "notify_project_team"
        self.policy.save()

        serialized_scope = core_utils.serialize_instance(self.policy.project)
        serialized_policy = core_utils.serialize_instance(self.policy)
        tasks.notify_about_limit_cost(serialized_scope, serialized_policy)

        mock_send_mail.assert_called_once()

        self.assertTrue(
            logging_models.Event.objects.filter(event_type="policy_notification")
        )

    @mock.patch("waldur_core.core.utils.send_mail")
    def test_notify_organization_owners(self, mock_send_mail):
        self.policy.actions = "notify_organization_owners"
        self.policy.save()

        serialized_scope = core_utils.serialize_instance(self.policy.project.customer)
        serialized_policy = core_utils.serialize_instance(self.policy)
        tasks.notify_about_limit_cost(serialized_scope, serialized_policy)

        mock_send_mail.assert_called_once()
        self.assertEqual(mock_send_mail.call_args.kwargs["to"][0], self.owner.email)

        self.assertTrue(
            logging_models.Event.objects.filter(event_type="policy_notification")
        )

    @mock.patch("waldur_mastermind.policy.policy_actions.tasks")
    def test_create_event_log(self, mock_tasks):
        self.policy.actions = "notify_organization_owners"
        self.policy.save()

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()

        mock_tasks.notify_about_limit_cost.delay.assert_called_once()
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

    def test_block_creation_of_new_resources(self):
        self.policy.actions = "block_creation_of_new_resources"
        self.policy.save()

        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()

        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_block_modification_of_existing_resources(self):
        self.policy.actions = "block_modification_of_existing_resources"
        self.policy.save()

        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        resource = marketplace_models.Resource.objects.get(
            uuid=response.data["marketplace_resource_uuid"]
        )

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()
        resource.set_state_ok()
        resource.save()

        self.client.force_authenticate(self.fixture.staff)
        url = marketplace_factories.ResourceFactory.get_url(resource, "update_limits")
        payload = {"limits": {"cpu": 2}}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_terminate_resources(self):
        self.policy.actions = "terminate_resources"
        self.policy.save()

        resource = self.fixture.resource
        resource.state = marketplace_models.Resource.States.OK
        resource.save()

        resource.offering.type = INSTANCE_TYPE
        resource.offering.save()

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()

        self.assertTrue(
            marketplace_models.Order.objects.filter(
                resource=resource,
                type=marketplace_models.Order.Types.TERMINATE,
            ).exists()
        )
        order = marketplace_models.Order.objects.filter(
            resource=resource,
            type=marketplace_models.Order.Types.TERMINATE,
        ).get()
        self.assertEqual(order.attributes, {"action": "force_destroy"})

    def test_request_downscaling(self):
        self.policy.actions = "request_downscaling"
        self.policy.created_by = self.fixture.user
        self.policy.save()

        resource = self.fixture.resource

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()

        resource.refresh_from_db()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)
        self.assertEqual(resource.requested_downscaling, True)
