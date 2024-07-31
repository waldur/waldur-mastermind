import datetime
from unittest import mock

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, plugins
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS
from waldur_mastermind.marketplace.tests.utils import TestCreateProcessor
from waldur_mastermind.marketplace_support import PLUGIN_NAME


class BaseOrderCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

    def create_order(self, user, offering=None, add_payload=None):
        if offering is None:
            offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        payload = {
            "project": structure_factories.ProjectFactory.get_url(self.project),
            "offering": factories.OfferingFactory.get_public_url(offering),
            "attributes": {},
        }

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)


@ddt
class OrderCreateTest(BaseOrderCreateTest):
    @data("staff", "owner", "admin", "manager")
    def test_user_can_create_order_in_valid_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    @data("user")
    def test_user_can_not_create_order_in_invalid_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_not_create_order_if_offering_is_not_available(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ARCHIVED)
        response = self.create_order(self.fixture.staff, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_order_with_plan(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": {},
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_can_not_create_order_if_offering_is_not_available_to_customer(self):
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, shared=False
        )
        offering.customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": {},
        }
        response = self.create_order(
            self.fixture.owner, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_not_create_order_with_plan_related_to_another_offering(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(),
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": {},
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_not_create_order_if_plan_max_amount_has_been_reached(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering, max_amount=3)
        factories.ResourceFactory.create_batch(3, plan=plan, offering=offering)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": {},
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_with_valid_attributes_specified_by_options(self):
        attributes = {
            "storage": 1000,
            "ram": 30,
            "cpu_count": 5,
        }
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, options=OFFERING_OPTIONS
        )
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": attributes,
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["attributes"], attributes)

    def test_order_creating_is_not_available_for_blocked_organization(self):
        user = self.fixture.owner
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_if_offering_is_not_shared(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.shared = False
        offering.customer = self.project.customer
        offering.save()
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "attributes": {},
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_user_cannot_create_order_in_project_is_expired(self):
        user = getattr(self.fixture, "staff")
        self.project.end_date = datetime.datetime(day=1, month=1, year=2020)
        self.project.save()

        with freeze_time("2020-01-01"):
            response = self.create_order(user)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_organization_groups_do_not_match_order_validation_fails(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        organization_group = structure_factories.OrganizationGroupFactory()
        offering.organization_groups.add(organization_group)

        response = self.create_order(user, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_organization_groups_match_order_validation_passes(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        organization_group = structure_factories.OrganizationGroupFactory()
        offering.organization_groups.add(organization_group)
        self.fixture.customer.organization_group = organization_group
        self.fixture.customer.save()

        response = self.create_order(user, offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())


@ddt
@mock.patch(
    "waldur_mastermind.marketplace.tasks.notify_consumer_about_pending_order.delay"
)
class OrderNotificationCreateTest(BaseOrderCreateTest):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)

        plugins.manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=TestCreateProcessor,
        )

        return super().setUp()

    def submit_public(self, role):
        provider_fixture = fixtures.ProjectFixture()
        consumer_fixture = fixtures.ProjectFixture()
        public_offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=True,
            billable=True,
            customer=provider_fixture.customer,
            type="TEST_TYPE",
        )
        return self.create_order(
            getattr(consumer_fixture, role),
            public_offering,
            add_payload={
                "project": structure_factories.ProjectFactory.get_url(
                    consumer_fixture.project
                ),
                "attributes": {"name": "test"},
                "plan": factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=public_offering)
                ),
            },
        )

    def test_notification_is_sent_when_order_is_created(self, mock_task):
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=True,
            billable=True,
            type="TEST_TYPE",
        )
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": {},
        }
        response = self.create_order(
            self.fixture.manager, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        mock_task.assert_called_once()

    @data("staff", "owner", "manager", "admin")
    def test_order_gets_approved_if_offering_is_private(self, role, mocked_task):
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=False,
            billable=False,
            customer=self.project.customer,
            type="TEST_TYPE",
        )

        response = self.create_order(getattr(self.fixture, role), offering)
        self.assertEqual(response.data["state"], "executing")
        mocked_task.assert_not_called()

    @data("staff", "owner")
    def test_public_offering_is_autoapproved_if_user_is_owner_or_staff(
        self, role, mocked_task
    ):
        response = self.submit_public(role)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["state"], "executing")
        mocked_task.assert_not_called()

    @data("manager", "admin")
    def test_public_offering_is_not_autoapproved_if_user_is_manager_or_admin(
        self, role, mocked_task
    ):
        response = self.submit_public(role)
        self.assertEqual(response.data["state"], "pending-consumer")
        mocked_task.assert_called()

    def test_public_offering_is_autoapproved_if_feature_is_enabled_for_manager(
        self, mocked_task
    ):
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        response = self.submit_public("manager")
        self.assertEqual(response.data["state"], "executing")
        mocked_task.assert_not_called()

    def test_public_offering_is_autoapproved_if_feature_is_enabled_for_admin(
        self, mocked_task
    ):
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        response = self.submit_public("admin")
        self.assertEqual(response.data["state"], "executing")
        mocked_task.assert_not_called()

    @data(True, False)
    def test_public_offering_is_approved_in_the_same_organization(
        self, auto_approve_in_service_provider_projects, mocked_task
    ):
        consumer_fixture = provider_fixture = fixtures.ProjectFixture()
        public_offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=True,
            billable=True,
            customer=provider_fixture.customer,
            type="TEST_TYPE",
            plugin_options={
                "auto_approve_in_service_provider_projects": auto_approve_in_service_provider_projects
            },
        )

        response = self.create_order(
            consumer_fixture.admin,
            public_offering,
            add_payload={
                "project": structure_factories.ProjectFactory.get_url(
                    consumer_fixture.project
                ),
                "attributes": {"name": "test"},
                "plan": factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=public_offering)
                ),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["state"],
            auto_approve_in_service_provider_projects
            and "executing"
            or "pending-consumer",
        )
        if auto_approve_in_service_provider_projects:
            mocked_task.assert_not_called()


@ddt
class OrderLimitsCreateTest(BaseOrderCreateTest):
    DEFAULT_LIMITS = {
        "storage": 1000,
        "ram": 30,
        "cpu_count": 5,
    }

    def test_user_can_not_create_order_with_invalid_attributes(self):
        attributes = {
            "storage": "invalid value",
        }
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, options=OFFERING_OPTIONS
        )
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": attributes,
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_with_valid_limits(self):
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, type=PLUGIN_NAME
        )
        plan = factories.PlanFactory(offering=offering)

        for key in self.DEFAULT_LIMITS.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            )

        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "limits": self.DEFAULT_LIMITS,
            "attributes": {},
        }

        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        order = models.Order.objects.last()
        self.assertEqual(order.limits["cpu_count"], 5)

    def test_user_can_not_create_order_with_invalid_limits(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        for key in self.DEFAULT_LIMITS.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.FIXED,
            )

        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "limits": self.DEFAULT_LIMITS,
            "attributes": {},
        }

        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(
        models.OfferingComponent.LimitPeriods.TOTAL,
        models.OfferingComponent.LimitPeriods.MONTH,
        models.OfferingComponent.LimitPeriods.ANNUAL,
    )
    def test_offering_limit_is_valid(self, limit_period):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        models.OfferingComponent.objects.create(
            offering=offering,
            type="cpu_count",
            billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            limit_amount=10,
            limit_period=limit_period,
        )

        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "limits": {"cpu_count": 5},
            "attributes": {},
        }

        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data(
        models.OfferingComponent.LimitPeriods.TOTAL,
        models.OfferingComponent.LimitPeriods.MONTH,
        models.OfferingComponent.LimitPeriods.ANNUAL,
    )
    def test_offering_limit_is_invalid(self, limit_period):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        models.OfferingComponent.objects.create(
            offering=offering,
            type="cpu_count",
            billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            limit_amount=1,
            limit_period=limit_period,
        )

        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "plan": factories.PlanFactory.get_public_url(plan),
            "limits": {"cpu_count": 5},
            "attributes": {},
        }

        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )


class OrderTermsOfServiceCreateTest(BaseOrderCreateTest):
    def test_user_can_create_order_if_terms_of_service_have_been_accepted(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.terms_of_service = "Terms of service"
        offering.save()
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "attributes": {},
            "accepting_terms_of_service": True,
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_user_can_create_order_if_terms_of_service_are_not_filled(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "attributes": {},
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_user_cannot_create_order_if_terms_of_service_have_been_not_accepted(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.terms_of_service = "Terms of service"
        offering.save()
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "attributes": {},
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, "utf-8"),
            '{"non_field_errors":["Terms of service for offering \'%s\' have not been accepted."]}'
            % offering,
        )
        self.assertFalse(models.Order.objects.filter(created_by=user).exists())


class OrderEndDateCreateTest(BaseOrderCreateTest):
    def test_set_end_date(self):
        user = self.fixture.staff
        response = self.create_order(
            user, add_payload={"attributes": {"end_date": "2025-01-01"}}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        resource = order.resource
        self.assertTrue(resource.end_date)
        self.assertEqual(resource.end_date_requested_by, user)

    def test_resource_end_date_set_to_default_if_required_but_not_provided(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
        }
        offering.save()

        response = self.create_order(self.fixture.owner, offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        resource = models.Resource.objects.last()
        end_date = resource.created + datetime.timedelta(days=7)
        self.assertEqual(resource.end_date, end_date.date())

    @freeze_time("2022-01-01")
    def test_resource_is_not_created_if_end_date_later_than_max_end_date(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        offering.save()
        end_date = datetime.date(2025, 12, 25)

        response = self.create_order(
            self.fixture.owner,
            offering,
            {"attributes": {"name": "test", "end_date": end_date}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_is_created_if_end_date_earlier_than_max_end_date(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)

        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        offering.save()
        end_date = datetime.date.today() + datetime.timedelta(days=10)

        response = self.create_order(
            self.fixture.owner,
            offering,
            {"attributes": {"name": "test", "end_date": end_date}},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        resource = models.Resource.objects.last()
        self.assertEqual(resource.end_date, end_date)

    @freeze_time("2022-01-01")
    def test_resource_is_not_created_if_end_date_later_than_latest_date_for_resource_termination(
        self,
    ):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "latest_date_for_resource_termination": "2030-01-01",
        }
        offering.save()
        end_date = datetime.date(2031, 12, 25)

        response = self.create_order(
            self.fixture.owner,
            offering,
            {"attributes": {"name": "test", "end_date": end_date}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class OrderDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.DESTROY_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.DESTROY_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.DESTROY_ORDER)

    @data("staff", "owner", "admin", "manager")
    def test_authorized_user_can_delete_order(self, user):
        response = self.delete_order(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(models.Order.objects.filter(project=self.project).exists())

    @data("user")
    def test_other_user_can_not_delete_order(self, user):
        response = self.delete_order(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(models.Order.objects.filter(created_by=self.manager).exists())

    def test_unauthorized_user_can_not_delete_order(self):
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_order_deleting_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.delete_order("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def delete_order(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.delete(url)
        return response


@ddt
class OrderFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        self.url = factories.OrderFactory.get_list_url()

    @data("staff", "owner", "admin", "manager")
    def test_orders_should_be_visible_to_colleagues_and_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data("user")
    def test_orders_should_be_invisible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_orders_should_be_invisible_to_unauthenticated_users(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filter_orders_for_service_manager(self):
        # Arrange
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        order = factories.OrderFactory(
            offering=offering, project=self.project, created_by=self.manager
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self.url, {"service_manager_uuid": self.fixture.user.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], order.uuid.hex)

    def test_service_provider_can_see_order(self):
        # Arrange
        user = structure_factories.UserFactory()
        self.order.offering.customer.add_user(user, CustomerRole.OWNER)

        # Act
        self.client.force_authenticate(user)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.order.uuid.hex)
