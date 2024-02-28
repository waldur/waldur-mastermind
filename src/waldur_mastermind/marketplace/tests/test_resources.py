import datetime
from unittest import mock

from constance.test.pytest import override_config
from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.utils import month_start
from waldur_core.logging import models as logging_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import ProjectFactory, UserFactory
from waldur_mastermind.common.utils import parse_date
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import callbacks, log, models, plugins
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import helpers as test_helpers
from waldur_mastermind.marketplace.tests import utils as test_utils
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_openstack.openstack.tests import factories as openstack_factories


class ResourceGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )

    def get_resource(self, user=None):
        if not user:
            user = self.fixture.owner
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(self.resource)
        return self.client.get(url)

    def test_resource_is_usage_based(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
        )

        self.assertTrue(self.get_resource().data["is_usage_based"])

    def test_resource_is_not_usage_based(self):
        self.assertFalse(self.get_resource().data["is_usage_based"])

    def test_project_manager_can_get_resource_data(self):
        response = self.get_resource(self.fixture.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_owner_can_get_resource_data(self):
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_service_provider_can_get_resource_data(self):
        owner = UserFactory()
        self.offering.customer.add_user(owner, CustomerRole.OWNER)

        response = self.get_resource()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_user_can_not_get_resource_data(self):
        response = self.get_resource(UserFactory())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_filter_resources_for_service_manager(self):
        # Arrange
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        resource = factories.ResourceFactory(project=self.project, offering=offering)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url()
        response = self.client.get(
            url, {"service_manager_uuid": self.fixture.user.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], resource.uuid.hex)

    def test_resource_contains_project_and_customer_data_after_project_deletion(self):
        expected_data = {
            "project_name": self.project.name,
            "project_uuid": self.project.uuid,
            "project_description": self.project.description,
            "customer_name": self.project.customer.name,
            "customer_uuid": self.project.customer.uuid,
        }

        self.project.delete()
        response_data = self.get_resource().data
        for key, value in expected_data.items():
            self.assertEqual(value, response_data[key])

    def test_username_is_fetched_for_current_user_and_offering(self):
        models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.manager, username="alice"
        )
        response = self.get_resource(self.fixture.manager)
        self.assertEqual(response.data["username"], "alice")

    def test_resource_data_includes_order_in_progress(self):
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("order_in_progress", response.data)
        self.assertIsNone(response.data["order_in_progress"])

    def test_resource_data_includes_order_info_for_existing_one(self):
        models.Order.objects.create(
            project=self.project,
            resource=self.resource,
            state=models.Order.States.EXECUTING,
            created_by=self.fixture.owner,
            offering=self.offering,
        )
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("order_in_progress", response.data)
        self.assertIsNotNone(response.data["order_in_progress"])


class ResourceSwitchPlanTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan1 = factories.PlanFactory()
        self.offering = self.plan1.offering
        self.plan2 = factories.PlanFactory(offering=self.offering)
        self.resource1 = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan1,
            state=models.Resource.States.OK,
        )
        self.resource2 = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SWITCH_RESOURCE_PLAN)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.SWITCH_RESOURCE_PLAN)
        ProjectRole.MANAGER.add_permission(PermissionEnum.SWITCH_RESOURCE_PLAN)

    def switch_plan(self, user, resource, plan):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(resource, "switch_plan")
        payload = {"plan": factories.PlanFactory.get_public_url(plan)}
        return self.client.post(url, payload)

    def test_plan_switch_is_available_if_plan_limit_is_not_reached(self):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_switch_is_available_if_resource_is_terminated(self):
        # Arrange
        self.resource2.state = models.Resource.States.TERMINATED
        self.resource2.save()

        self.plan2.max_amount = 1
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_switch_is_not_available_if_plan_limit_has_been_reached(self):
        # Arrange
        self.plan2.max_amount = 1
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switch_is_not_available_if_plan_is_related_to_another_offering(self):
        # Act
        response = self.switch_plan(
            self.fixture.owner, self.resource1, factories.PlanFactory()
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switch_is_not_available_if_resource_is_not_OK(self):
        # Arrange
        self.resource1.state = models.Resource.States.UPDATING
        self.resource1.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_order_is_created(self):
        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.Order.objects.filter(
                type=models.Order.Types.UPDATE,
                plan=self.plan2,
                resource=self.resource1,
            ).exists()
        )

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.created_by, self.fixture.staff)

    def test_plan_switch_is_not_allowed_if_pending_order_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderFactory(
            resource=self.resource1, state=models.Order.States.PENDING_CONSUMER
        )

        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switching_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order")
    def test_order_has_been_approved_if_user_has_got_permissions(self, mock_task):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(
            "marketplace.order:%s" % order.id, "core.user:%s" % self.fixture.owner.id
        )

    @mock.patch("waldur_mastermind.marketplace.views.tasks")
    def test_order_has_not_been_approved_if_user_has_not_got_permissions(
        self, mock_tasks
    ):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.admin, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_not_called()


@ddt
class ResourceTerminateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=models.Resource.States.OK,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)

    def terminate(self, user, attributes=None):
        attributes = attributes or {}
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(self.resource, "terminate")
        if attributes:
            return self.client.post(url, {"attributes": attributes})
        else:
            return self.client.post(url)

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.notify_consumer_about_pending_order.delay"
    )
    def test_service_provider_can_terminate_resource(self, mocked_approve):
        # Arrange
        owner = UserFactory()
        self.offering.customer.add_user(owner, CustomerRole.OWNER)

        # Act
        response = self.terminate(owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_approve.assert_not_called()

    def test_order_is_created_when_user_submits_termination_request(self):
        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.project, self.project)

    @data(
        models.Resource.States.CREATING,
        models.Resource.States.UPDATING,
        models.Resource.States.TERMINATING,
    )
    def test_termination_request_is_not_accepted_if_resource_is_not_ok_or_erred(
        self, state
    ):
        # Arrange
        self.resource.state = state
        self.resource.save()

        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data(models.Resource.States.OK, models.Resource.States.ERRED)
    def test_termination_request_is_accepted_if_resource_is_ok_or_erred(self, state):
        # Arrange
        self.resource.state = state
        self.resource.save()

        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.created_by, self.fixture.staff)

    def test_plan_switch_is_not_allowed_if_pending_order_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderFactory(
            resource=self.resource, state=models.Order.States.PENDING_CONSUMER
        )

        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_terminating_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.terminate(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_options_can_be_passed_if_resource_is_terminated(self):
        # Act
        response = self.terminate(self.fixture.staff, {"param": True})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.project, self.project)
        self.assertTrue(order.attributes.get("param"))

    def test_user_can_terminate_resource_if_project_has_been_soft_deleted(self):
        self.project.is_removed = True
        self.project.save()
        response = self.terminate(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class PlanUsageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan1 = factories.PlanFactory()
        self.offering = self.plan1.offering
        self.plan2 = factories.PlanFactory(offering=self.offering)

        factories.ResourceFactory.create_batch(
            3,
            project=self.project,
            offering=self.offering,
            plan=self.plan1,
            state=models.Resource.States.OK,
        )

        factories.ResourceFactory.create_batch(
            2,
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
            state=models.Resource.States.OK,
        )

        factories.ResourceFactory.create_batch(
            2,
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
            state=models.Resource.States.TERMINATED,
        )

    def get_stats(self, data=None):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.PlanFactory.get_list_url("usage_stats")
        response = self.client.get(url, data)
        return response

    def test_count_plans_for_ok_resources(self):
        response = self.get_stats()
        self.assertEqual(response.data[0]["offering_uuid"], self.offering.uuid)
        self.assertEqual(
            response.data[0]["customer_provider_uuid"], self.offering.customer.uuid
        )
        self.assertEqual(response.data[0]["plan_uuid"], self.plan1.uuid)
        self.assertEqual(response.data[0]["usage"], 3)

    def test_count_plans_for_terminated_resources(self):
        response = self.get_stats()
        self.assertEqual(response.data[1]["usage"], 2)

    def test_order_by_remaining_ascending(self):
        self.plan1.max_amount = 100
        self.plan1.save()

        self.plan2.max_amount = 10
        self.plan2.save()

        response = self.get_stats({"o": "remaining"})
        data = response.data

        self.assertEqual(data[0]["remaining"], 10 - 2)
        self.assertEqual(data[1]["remaining"], 100 - 3)

    def test_order_by_remaining_descending(self):
        self.plan1.max_amount = 100
        self.plan1.save()

        self.plan2.max_amount = 10
        self.plan2.save()

        response = self.get_stats({"o": "-remaining"})
        data = response.data

        self.assertEqual(data[0]["remaining"], 100 - 3)
        self.assertEqual(data[1]["remaining"], 10 - 2)

    def test_filter_plans_by_offering_uuid(self):
        plan = factories.PlanFactory()

        factories.ResourceFactory.create_batch(
            4,
            project=self.project,
            offering=plan.offering,
            plan=plan,
            state=models.Resource.States.OK,
        )

        response = self.get_stats({"offering_uuid": plan.offering.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["usage"], 4)
        self.assertEqual(response.data[0]["offering_uuid"], plan.offering.uuid)

    def test_filter_plans_by_customer_provider_uuid(self):
        plan = factories.PlanFactory()

        factories.ResourceFactory.create_batch(
            4,
            project=self.project,
            offering=plan.offering,
            plan=plan,
            state=models.Resource.States.OK,
        )

        response = self.get_stats(
            {"customer_provider_uuid": plan.offering.customer.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["usage"], 4)
        self.assertEqual(
            response.data[0]["customer_provider_uuid"], plan.offering.customer.uuid
        )


class ResourceCostEstimateTest(test.APITransactionTestCase):
    @override_config(
        WALDUR_SUPPORT_ENABLED=True,
        WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    )
    def test_when_order_is_processed_cost_estimate_is_initialized(self):
        # Arrange
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(type=PLUGIN_NAME)
        plan = factories.PlanFactory(unit_price=10)

        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            attributes={"name": "item_name", "description": "Description"},
            state=models.Order.States.EXECUTING,
        )

        # Act
        marketplace_utils.process_order(order, fixture.staff)

        # Assert
        order.refresh_from_db()
        self.assertEqual(order.resource.cost, plan.unit_price)

    def test_initialization_cost_is_added_to_cost_estimate_for_creation_request(self):
        # Arrange
        offering = factories.OfferingFactory(type=PLUGIN_NAME)
        one_time_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.ONE_TIME,
            type="signup",
        )
        usage_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
            type="cpu",
        )

        plan = factories.PlanFactory()
        factories.PlanComponentFactory(
            plan=plan, component=one_time_offering_component, price=100
        )
        factories.PlanComponentFactory(
            plan=plan, component=usage_offering_component, price=10
        )

        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
        )
        order.init_cost()
        self.assertEqual(order.cost, 100)

    def test_when_plan_is_switched_cost_estimate_is_updated(self):
        # Arrange
        old_plan = factories.PlanFactory(unit_price=10)
        new_plan = factories.PlanFactory(unit_price=100)
        resource = factories.ResourceFactory(plan=old_plan)

        factories.OrderFactory(
            state=models.Order.States.EXECUTING,
            type=models.Order.Types.UPDATE,
            resource=resource,
            plan=new_plan,
        )

        # Act
        callbacks.resource_update_succeeded(resource)
        resource.refresh_from_db()

        # Assert
        self.assertEqual(resource.cost, new_plan.unit_price)

    def test_plan_switch_cost_is_added_to_cost_estimate_for_order(self):
        # Arrange
        offering = factories.OfferingFactory(type=PLUGIN_NAME)
        switch_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH,
            type="plan_switch",
        )
        usage_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
            type="cpu",
        )

        plan = factories.PlanFactory()
        factories.PlanComponentFactory(
            plan=plan, component=switch_offering_component, price=50
        )
        factories.PlanComponentFactory(
            plan=plan, component=usage_offering_component, price=10
        )

        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            type=models.Order.Types.UPDATE,
        )
        order.init_cost()
        self.assertEqual(order.cost, 50)


@ddt
class ResourceNotificationTest(test.APITransactionTestCase):
    @data(
        "log_resource_creation_succeeded",
        "log_resource_creation_failed",
        "log_resource_update_succeeded",
        "log_resource_update_failed",
        "log_resource_terminate_succeeded",
        "log_resource_terminate_failed",
    )
    @mock.patch("waldur_mastermind.marketplace.log.tasks")
    def test_notify_about_resource_change(self, log_func_name, mock_tasks):
        resource = factories.ResourceFactory()
        log_func = getattr(log, log_func_name)
        log_func(resource)
        if log_func_name != "log_resource_update_succeeded":
            mock_tasks.notify_about_resource_change.delay.assert_called_once()
        else:
            mock_tasks.notify_about_resource_change.delay.assert_not_called()


class ResourceUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_url(self.resource)

    def make_request(self, user, payload=None):
        self.client.force_authenticate(user)
        payload = payload or {"name": "new name", "description": "new description"}
        return self.client.patch(self.url, payload)

    def test_authorized_user_can_update_resource(self):
        response = self.make_request(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, "new name")
        self.assertEqual(self.resource.description, "new description")

    def test_unauthorized_user_can_not_update_resource(self):
        response = self.make_request(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_renaming_of_resource_should_generate_audit_log(self):
        old_name = self.resource.name
        response = self.make_request(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertTrue(
            logging_models.Event.objects.filter(
                message=f"Marketplace resource {self.resource.name} has been renamed. Old name: {old_name}."
            ).exists()
        )

    def test_authorized_user_can_update_end_date(self):
        with freeze_time("2020-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2021-01-01"})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertTrue(self.resource.end_date)
            self.assertEqual(self.resource.end_date_requested_by, self.fixture.staff)

    @test_helpers.override_marketplace_settings(ENABLE_RESOURCE_END_DATE=False)
    def test_user_can_not_update_end_date_if_feature_is_disabled(self):
        response = self.make_request(self.fixture.staff, {"end_date": "2021-01-01"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_authorized_user_can_set_current_past_date(self):
        with freeze_time("2020-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2020-01-01"})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertTrue(self.resource.end_date)

    def test_user_cannot_set_past_date(self):
        with freeze_time("2022-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2020-01-01"})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_end_date_should_generate_audit_log(self):
        with freeze_time("2020-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2021-01-01"})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertTrue(
                logging_models.Event.objects.filter(
                    message=f"End date of marketplace resource {self.resource.name} has been updated. End date: {self.resource.end_date}. User: {self.fixture.staff}."
                ).exists()
            )

    def test_resource_end_date_is_set_to_default_termination_if_required_and_not_provided(
        self,
    ):
        self.fixture.resource.offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
        }
        self.fixture.resource.offering.save()
        payload = {
            "name": "resource name update",
        }
        response = self.make_request(self.fixture.staff, payload)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["end_date"], self.fixture.resource.end_date)

    def test_end_date_is_not_updated_if_later_than_max_end_date(self):
        self.fixture.resource.offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        self.fixture.resource.offering.save()
        end_date = self.fixture.resource.created + datetime.timedelta(days=50)
        end_date = end_date.date()
        payload = {
            "end_date": end_date,
        }
        response = self.make_request(self.fixture.staff, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_date_is_updated_if_earlier_than_max_end_date(self):
        self.fixture.resource.offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        self.fixture.resource.offering.save()
        end_date = self.fixture.resource.created + datetime.timedelta(days=15)
        end_date = end_date.date()
        payload = {
            "end_date": end_date,
        }
        response = self.make_request(self.fixture.staff, payload)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["end_date"], self.fixture.resource.end_date, end_date
        )

    def test_end_date_is_not_updated_if_later_than_latest_date_for_resource_termination(
        self,
    ):
        with freeze_time("2022-01-01"):
            self.fixture.resource.offering.plugin_options = {
                "is_resource_termination_date_required": True,
                "default_resource_termination_offset_in_days": 7,
                "latest_date_for_resource_termination": "2030-01-01",
            }
            self.fixture.resource.offering.save()
            end_date = "2031-01-01"
            payload = {
                "end_date": end_date,
            }
            response = self.make_request(self.fixture.staff, payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class ResourceSetEndDateByProviderTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_url(
            self.resource, "set_end_date_by_provider"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_END_DATE)
        CustomerRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_END_DATE)

    def make_request(self, user, payload):
        self.client.force_authenticate(user)
        return self.client.post(self.url, payload)

    @test_helpers.override_marketplace_settings(ENABLE_RESOURCE_END_DATE=False)
    def test_user_can_not_update_end_date_if_feature_is_disabled(self):
        response = self.make_request(
            self.fixture.offering_owner, {"end_date": "2021-01-01"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2020-01-01")
    def test_resource_is_not_used_for_last_3_months_and_end_date_is_7_days_in_future(
        self,
    ):
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                self.fixture.offering_owner, {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertEqual(self.resource.end_date, parse_date("2020-05-08"))

            self.assertTrue(
                logging_models.Event.objects.filter(
                    message__contains="End date of marketplace resource %s has been updated by provider."
                    % self.resource.name
                ).exists()
            )

    @freeze_time("2020-01-01")
    def test_resource_is_not_used_for_last_3_months_and_end_date_is_not_7_days_in_future(
        self,
    ):
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                self.fixture.offering_owner, {"end_date": "2020-05-05"}
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2020-01-01")
    def test_resource_is_used_for_last_3_months_and_end_date_is_not_7_days_in_future(
        self,
    ):
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        response = self.make_request(
            self.fixture.offering_owner, {"end_date": "2020-01-05"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2020-01-01")
    def test_resource_is_used_for_last_3_months_and_end_date_is_more_than_7_days_in_future(
        self,
    ):
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        response = self.make_request(
            self.fixture.offering_owner, {"end_date": "2020-01-10"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("staff", "offering_owner", "service_manager", "global_support")
    @freeze_time("2020-01-01")
    def test_permission_positive(self, user):
        self.resource.state = models.Resource.States.OK
        self.resource.save()

        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertEqual(
                self.resource.end_date_requested_by, getattr(self.fixture, user)
            )

    @data("admin", "manager", "member", "owner", "customer_support")
    @freeze_time("2020-01-01")
    def test_permission_negative(self, user):
        self.resource.state = models.Resource.States.OK
        self.resource.save()

        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ResourceSetEndDateByStaffTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_url(
            self.resource, "set_end_date_by_staff"
        )

    def make_request(self, user, payload):
        self.client.force_authenticate(user)
        return self.client.post(self.url, payload)

    @test_helpers.override_marketplace_settings(ENABLE_RESOURCE_END_DATE=False)
    def test_user_can_not_update_end_date_if_feature_is_disabled(self):
        response = self.make_request(self.fixture.staff, {"end_date": "2021-01-01"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2020-01-01")
    @data(
        "staff",
    )
    def test_user_can_set_end_date(self, user):
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertEqual(self.resource.end_date, parse_date("2020-05-08"))

            self.assertTrue(
                logging_models.Event.objects.filter(
                    message__contains="End date of marketplace resource %s has been updated by staff."
                    % self.resource.name
                ).exists()
            )
            self.resource.refresh_from_db()
            self.assertEqual(
                self.resource.end_date_requested_by, getattr(self.fixture, user)
            )

    @freeze_time("2020-01-01")
    @data("offering_owner", "service_manager")
    def test_user_cannot_set_end_date(self, user):
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ResourceUpdateLimitsTest(test.APITransactionTestCase):
    def setUp(self):
        plugins.manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=test_utils.TestCreateProcessor,
            update_resource_processor=test_utils.TestUpdateScopedProcessor,
            can_update_limits=True,
        )

        self.fixture = fixtures.ServiceFixture()
        self.resource = factories.ResourceFactory()
        self.resource.state = models.Resource.States.OK
        self.resource.project.customer = self.fixture.customer
        self.resource.project.save()
        self.resource.limits = {"vcpu": 1}
        self.resource.save()
        self.resource.offering.type = "TEST_TYPE"
        self.resource.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

    def update_limits(self, user, resource, limits=None):
        limits = limits or {"vcpu": 10}
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(resource, "update_limits")
        payload = {"limits": limits}
        return self.client.post(url, payload)

    def test_create_update_limits_order(self):
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_update_limits_is_not_available_if_resource_is_not_OK(self):
        # Arrange
        self.resource.state = models.Resource.States.UPDATING
        self.resource.save()

        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_order_is_created(self):
        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.Order.objects.filter(
                type=models.Order.Types.UPDATE,
                resource=self.resource,
            ).exists()
        )

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.update_limits(self.fixture.staff, self.resource)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.created_by, self.fixture.staff)

    def test_update_limits_is_not_allowed_if_pending_order_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderFactory(
            resource=self.resource, state=models.Order.States.PENDING_CONSUMER
        )

        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_limits_is_not_available_for_blocked_organization(self):
        customer = self.resource.project.customer
        customer.blocked = True
        customer.save()
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order")
    def test_order_has_been_approved_if_user_has_got_permissions(self, mock_task):
        # Act
        response = self.update_limits(self.fixture.staff, self.resource)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(
            "marketplace.order:%s" % order.id, "core.user:%s" % self.fixture.staff.id
        )

    @mock.patch("waldur_mastermind.marketplace.views.tasks")
    def test_order_has_not_been_approved_if_user_has_not_got_permissions(
        self, mock_tasks
    ):
        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_not_called()

    def test_update_limit_process(self):
        response = self.update_limits(self.fixture.staff, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(
            type=models.Order.Types.UPDATE,
            state=models.Order.States.EXECUTING,
            resource=self.resource,
        )
        marketplace_utils.process_order(order, self.fixture.staff)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["vcpu"], 10)

    def test_impossible_set_the_same_limits(self):
        response = self.update_limits(self.fixture.owner, self.resource, {"vcpu": 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResourceMoveTest(test.APITransactionTestCase):
    def setUp(self):
        self.tenant = openstack_factories.TenantFactory()
        self.fixture = fixtures.ProjectFixture()
        self.new_project = ProjectFactory()
        self.project = self.fixture.project

        self.resource = factories.ResourceFactory(project=self.project)
        self.resource.scope = self.tenant
        self.resource.save()

        self.url = factories.ResourceFactory.get_url(
            self.resource, action="move_resource"
        )

    def get_response(self, role):
        self.client.force_authenticate(role)
        payload = {"project": {"url": ProjectFactory.get_url(self.new_project)}}
        return self.client.post(self.url, payload)

    def test_move_resource_rest(self):
        response = self.get_response(self.fixture.staff)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.resource.project, self.new_project)

    def test_move_resource_is_not_possible_for_project_owner(self):
        response = self.get_response(self.fixture.owner)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.resource.project, self.project)

    def test_move_resource_is_not_possible_when_new_customer_is_blocked(self):
        new_customer = self.new_project.customer
        new_customer.blocked = True
        new_customer.save()

        response = self.get_response(self.fixture.staff)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.resource.project, self.project)

    def test_move_resource_exception_handling(self):
        start_invoice = invoices_factories.InvoiceFactory(
            customer=self.project.customer,
            year=2020,
            month=1,
            state=invoices_models.Invoice.States.PENDING,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=start_invoice,
            project=self.project,
            resource=self.resource,
        )

        invoices_factories.InvoiceFactory(
            customer=self.new_project.customer,
            year=2020,
            month=1,
            state=invoices_models.Invoice.States.CREATED,
        )

        response = self.get_response(self.fixture.staff)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.json(),
            {
                "error_message": "Resource moving is not possible, because invoice items moving is not possible."
            },
        )
        self.assertEqual(self.resource.project, self.project)


@ddt
class ResourceBackendIDTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_url(
            self.resource, action="set_backend_id"
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_ID)

    def make_request(self, role):
        self.client.force_authenticate(role)
        payload = {"backend_id": "new_backend_id"}
        return self.client.post(self.url, payload)

    @data("staff", "offering_owner", "service_owner")
    def test_user_can_set_backend_id_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_id, "new_backend_id")

    @data("owner", "admin", "manager")
    def test_user_can_not_set_backend_id_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ResourceReportTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.resource = factories.ResourceFactory(project=self.project)
        self.url = factories.ResourceFactory.get_url(
            self.resource, action="submit_report"
        )
        self.valid_report = [{"header": "Section header", "body": "Section body"}]

        service_manager = UserFactory()
        self.resource.offering.customer.add_user(
            service_manager, role=CustomerRole.MANAGER
        )
        setattr(self.fixture, "service_manager", service_manager)

        service_owner = UserFactory()
        self.resource.offering.customer.add_user(service_owner, role=CustomerRole.OWNER)
        setattr(self.fixture, "service_owner", service_manager)
        CustomerRole.OWNER.add_permission(PermissionEnum.SUBMIT_RESOURCE_REPORT)
        CustomerRole.MANAGER.add_permission(PermissionEnum.SUBMIT_RESOURCE_REPORT)

    def make_request(self, role, payload):
        self.client.force_authenticate(role)
        return self.client.post(self.url, {"report": payload})

    @data("staff", "service_owner", "service_manager")
    def test_user_can_submit_report(self, user):
        response = self.make_request(getattr(self.fixture, user), self.valid_report)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.report, self.valid_report)

    def test_admin_can_not_submit_report(self):
        response = self.make_request(self.fixture.admin, self.valid_report)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_report_should_contain_at_least_one_section(self):
        response = self.make_request(self.fixture.staff, [])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_section_should_contain_header_and_body(self):
        response = self.make_request(self.fixture.staff, [1, 2])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResourceDetailsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering
        )

    def make_request(self):
        url = factories.ResourceFactory.get_url(self.resource, action="details")
        self.client.force_authenticate(self.fixture.user)
        return self.client.get(url)

    def test_resource_without_scope_returns_error_404(self):
        response = self.make_request()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_resource_with_scope_returns_valid_resource_details(self):
        self.resource.scope = openstack_factories.TenantFactory(project=self.project)
        self.resource.save()
        response = self.make_request()
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ResourceGetTeamTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.service_owner = self.fixture.owner
        self.admin = self.fixture.admin

        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering
        )

        self.url = factories.ResourceFactory.get_url(self.resource, action="team")
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCE_USERS)

    def test_service_owner_can_get_resource_team(self):
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.url)
        users = response.data
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(users))
        user = users[0]
        self.assertEqual(self.admin.full_name, user["full_name"])

    def test_user_can_not_get_resource_team(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url)
        self.assertEqual(403, response.status_code)


class ResourceUsageLimitsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.user = self.fixture.staff

        self.resource = factories.ResourceFactory()
        self.resource.state = models.Resource.States.OK
        self.resource.limits = {"cpu": 100}
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.resource.offering,
            billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            limit_period=models.OfferingComponent.LimitPeriods.TOTAL,
        )
        self.resource.plan = factories.PlanFactory(offering=self.resource.offering)
        factories.PlanComponentFactory(
            component=self.offering_component,
            plan=self.resource.plan,
        )
        self.resource.save()

        self.url = factories.ResourceFactory.get_url(self.resource)

        factories.ComponentUsageFactory(
            resource=self.resource, component=self.offering_component, usage=10
        )
        new_date = datetime.datetime(
            year=datetime.date.today().year - 1, month=1, day=1
        )
        factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.offering_component,
            usage=5,
            date=new_date,
            billing_period=month_start(new_date),
        )

    def test_if_limit_period_is_total(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limit_usage"], {"cpu": 15})

    def test_if_limit_period_is_annual(self):
        self.offering_component.limit_period = (
            models.OfferingComponent.LimitPeriods.ANNUAL
        )
        self.offering_component.save()

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limit_usage"], {"cpu": 10})

    def test_if_limit_period_is_null(self):
        self.offering_component.limit_period = None
        self.offering_component.save()

        self.resource.current_usages = {"cpu": 5}
        self.resource.save()

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limit_usage"], {"cpu": 5})


class DownscalingRequestCompletedTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.service_owner = self.fixture.owner
        self.admin = self.fixture.admin

        self.resource = factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            requested_downscaling=True,
        )

        self.url = factories.ResourceFactory.get_url(
            self.resource, action="downscaling_request_completed"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.COMPLETE_RESOURCE_DOWNSCALING)

    def test_service_owner_can_downscaling_request_completed(self):
        self.client.force_authenticate(self.service_owner)

        response = self.client.post(self.url)
        self.assertEqual(200, response.status_code)

        self.assertTrue(
            logging_models.Event.objects.filter(
                message__contains="Resource %s has been downscaled."
                % self.resource.name
            ).exists()
        )

    def test_validate_downscaling_requesting(self):
        self.resource.requested_downscaling = False
        self.resource.save()
        self.client.force_authenticate(self.service_owner)

        response = self.client.post(self.url)
        self.assertEqual(400, response.status_code)

    def test_user_can_not_downscaling_request_completed(self):
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url)
        self.assertEqual(403, response.status_code)
