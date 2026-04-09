import datetime
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from ddt import data, ddt, unpack
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.models import NAME_LENGTH
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, plugins
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS
from waldur_mastermind.marketplace.tests.utils import TestCreateProcessor


class BaseOrderCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        CustomerRole.READER.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.CREATE_ORDER)

    def create_order(self, user, offering=None, add_payload=None, skip_auto_plan=False):
        if offering is None:
            offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        payload = {
            "project": structure_factories.ProjectFactory.get_url(self.project),
            "offering": factories.OfferingFactory.get_public_url(offering),
            "attributes": {},
        }

        if add_payload:
            payload.update(add_payload)

        if "plan" not in payload and not skip_auto_plan:
            plan = offering.plans.filter(archived=False).first()
            if plan:
                payload["plan"] = factories.PlanFactory.get_public_url(plan)
            else:
                # Create a plan if offering doesn't have one
                plan = factories.PlanFactory(offering=offering)
                payload["plan"] = factories.PlanFactory.get_public_url(plan)

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

    @data(OfferingStates.ARCHIVED, OfferingStates.UNAVAILABLE)
    def test_user_can_not_create_order_if_offering_is_not_available(self, state):
        offering = factories.OfferingFactory(state=state)
        response = self.create_order(self.fixture.staff, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_order_with_plan(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
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
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE, shared=False)
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

    def test_staff_can_override_private_offering_restriction(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE, shared=False)
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
        self.assertTrue(
            models.Order.objects.filter(created_by=self.fixture.staff).exists()
        )

    def test_can_not_create_order_without_plan(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        factories.PlanFactory(offering=offering, archived=False)
        response = self.create_order(self.fixture.staff, offering, skip_auto_plan=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plan", response.data)
        self.assertIn(
            "Plan is required when creating resources",
            str(response.data["plan"]),
        )

    def test_can_not_create_order_with_plan_related_to_another_offering(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
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
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
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
            state=OfferingStates.ACTIVE, options=OFFERING_OPTIONS
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

    def test_resource_options_are_set_during_order_creation(self):
        """Test that resource options are populated from offering resource_options during order creation."""
        attributes = {
            "cpu": 2,
            "ram": 1024,
            "storage": 500,  # This should not be in options
        }
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.resource_options = {
            "options": {
                "cpu": None,
                "ram": None,
            },  # Only cpu and ram are resource options
            "order": [],
        }
        offering.save()

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

        # Check that the created resource has the correct options
        order = models.Order.objects.get(uuid=response.data["uuid"])
        resource = order.resource

        # Verify resource options contain only the options defined in offering.resource_options
        self.assertTrue(isinstance(resource.options, dict))
        self.assertEqual(resource.options["cpu"], 2)
        self.assertEqual(resource.options["ram"], 1024)
        self.assertNotIn(
            "storage", resource.options
        )  # storage is not in resource_options

    def test_order_creating_is_not_available_for_blocked_organization(self):
        user = self.fixture.owner
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_if_offering_is_not_shared(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
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
        user = self.fixture.staff
        self.project.end_date = datetime.datetime(day=1, month=1, year=2020)
        self.project.save()

        with freeze_time("2020-01-01"):
            response = self.create_order(user)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_organization_groups_do_not_match_order_validation_fails(self):
        user = self.fixture.owner
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        organization_group = structure_factories.OrganizationGroupFactory()
        offering.organization_groups.add(organization_group)

        response = self.create_order(user, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_can_override_organization_group_restriction(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        organization_group = structure_factories.OrganizationGroupFactory()
        offering.organization_groups.add(organization_group)

        response = self.create_order(user, offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_if_organization_groups_match_order_validation_passes(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        organization_group = structure_factories.OrganizationGroupFactory()
        offering.organization_groups.add(organization_group)
        self.fixture.customer.organization_groups.add(organization_group)

        response = self.create_order(user, offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_creation_fails_if_project_team_count_not_satisfied(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {"minimal_team_count_for_provisioning": 1}
        offering.save()

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_creation_succeeds_if_project_team_count_satisfied(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {"minimal_team_count_for_provisioning": 1}
        offering.save()
        self.fixture.admin

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_creation_fails_if_project_role_requirement_not_satisfied(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "required_team_role_for_provisioning": ProjectRole.ADMIN.name
        }
        offering.save()

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_creation_succeeds_if_project_role_requirement_satisfied(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "required_team_role_for_provisioning": ProjectRole.ADMIN.name
        }
        offering.save()
        self.fixture.admin

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_creation_fails_if_maximal_resource_count_per_project_is_reached(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"maximal_resource_count_per_project": 1},
        )
        factories.ResourceFactory(project=self.project, offering=offering)

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "the maximum number of resources", response.data["non_field_errors"][0]
        )

    def test_creation_succeeds_if_maximal_resource_count_per_project_is_not_reached(
        self,
    ):
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"maximal_resource_count_per_project": 2},
        )
        plan = factories.PlanFactory(offering=offering)
        factories.ResourceFactory(project=self.project, offering=offering, plan=plan)

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_terminated_resources_are_not_counted_towards_limit(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"maximal_resource_count_per_project": 1},
        )
        plan = factories.PlanFactory(offering=offering)
        factories.ResourceFactory(
            project=self.project,
            offering=offering,
            plan=plan,
            state=models.Resource.States.TERMINATED,
        )

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_resources_in_other_projects_are_not_counted_towards_limit(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"maximal_resource_count_per_project": 1},
        )
        plan = factories.PlanFactory(offering=offering)
        # Create resource in a different project
        factories.ResourceFactory(
            project=structure_factories.ProjectFactory(), offering=offering, plan=plan
        )

        response = self.create_order(user, offering)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_creation_fails_if_resource_with_same_attribute_value_exists(self):
        """Test that unique_resource_per_attribute prevents duplicate attribute values."""
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"unique_resource_per_attribute": "storage_data_type"},
        )
        plan = factories.PlanFactory(offering=offering)
        # Create existing resource with storage_data_type=Store
        factories.ResourceFactory(
            project=self.project,
            offering=offering,
            plan=plan,
            attributes={"storage_data_type": "Store"},
        )

        # Try to create another resource with same storage_data_type=Store
        response = self.create_order(
            user, offering, add_payload={"attributes": {"storage_data_type": "Store"}}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "already has a resource with storage_data_type='Store'",
            response.data["non_field_errors"][0],
        )

    def test_creation_succeeds_with_different_attribute_value(self):
        """Test that different attribute values are allowed."""
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"unique_resource_per_attribute": "storage_data_type"},
        )
        plan = factories.PlanFactory(offering=offering)
        # Create existing resource with storage_data_type=Store
        factories.ResourceFactory(
            project=self.project,
            offering=offering,
            plan=plan,
            attributes={"storage_data_type": "Store"},
        )

        # Create another resource with storage_data_type=Archive (different value)
        response = self.create_order(
            user, offering, add_payload={"attributes": {"storage_data_type": "Archive"}}
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_terminated_resource_does_not_block_same_attribute_value(self):
        """Test that terminated resources don't block new resources with same attribute."""
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"unique_resource_per_attribute": "storage_data_type"},
        )
        plan = factories.PlanFactory(offering=offering)
        # Create terminated resource with storage_data_type=Store
        factories.ResourceFactory(
            project=self.project,
            offering=offering,
            plan=plan,
            attributes={"storage_data_type": "Store"},
            state=models.Resource.States.TERMINATED,
        )

        # Should be able to create new resource with same storage_data_type=Store
        response = self.create_order(
            user, offering, add_payload={"attributes": {"storage_data_type": "Store"}}
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unique_resource_per_attribute_not_enforced_without_config(self):
        """Test that validation is skipped when plugin option is not set."""
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={},  # No unique_resource_per_attribute
        )
        plan = factories.PlanFactory(offering=offering)
        # Create existing resource with storage_data_type=Store
        factories.ResourceFactory(
            project=self.project,
            offering=offering,
            plan=plan,
            attributes={"storage_data_type": "Store"},
        )

        # Should be able to create duplicate since config is not set
        response = self.create_order(
            user, offering, add_payload={"attributes": {"storage_data_type": "Store"}}
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unique_resource_per_attribute_skipped_when_attribute_not_provided(self):
        """Test that validation is skipped when attribute is not in order."""
        user = self.fixture.staff
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            plugin_options={"unique_resource_per_attribute": "storage_data_type"},
        )
        plan = factories.PlanFactory(offering=offering)
        # Create existing resource with storage_data_type=Store
        factories.ResourceFactory(
            project=self.project,
            offering=offering,
            plan=plan,
            attributes={"storage_data_type": "Store"},
        )

        # Create order without storage_data_type attribute
        response = self.create_order(
            user, offering, add_payload={"attributes": {"other_attr": "value"}}
        )

        # Should succeed (attribute validation for required fields is separate)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class OrderCreatePermissionTest(BaseOrderCreateTest):
    """Tests that CREATE_ORDER permission gates order creation correctly."""

    def test_customer_reader_cannot_create_order(self):
        user = structure_factories.UserFactory()
        self.fixture.customer.add_user(user, CustomerRole.READER)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_reader_with_create_order_permission_can_create_order(self):
        CustomerRole.READER.add_permission(PermissionEnum.CREATE_ORDER)
        user = structure_factories.UserFactory()
        self.fixture.customer.add_user(user, CustomerRole.READER)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("owner", "admin", "manager", "member")
    def test_authorized_user_can_create_order(self, user_role):
        user = getattr(self.fixture, user_role)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_can_create_order_bypassing_permission_check(self):
        response = self.create_order(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_unrelated_user_without_project_access_cannot_create_order(self):
        user = structure_factories.UserFactory()
        response = self.create_order(user)
        # Rejected at serializer project validation (400), before permission check
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


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
        provider_fixture = structure_fixtures.ProjectFixture()
        consumer_fixture = structure_fixtures.ProjectFixture()
        public_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
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
            state=OfferingStates.ACTIVE,
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
            state=OfferingStates.ACTIVE,
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
        consumer_fixture = provider_fixture = structure_fixtures.ProjectFixture()
        public_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
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

    @data(
        (True, False, "executing"),  # auto_approve=True, disable=False -> auto-approved
        (
            True,
            True,
            "pending-consumer",
        ),  # auto_approve=True, disable=True -> manual approval
        (
            False,
            False,
            "pending-consumer",
        ),  # auto_approve=False, disable=False -> manual approval
        (
            False,
            True,
            "pending-consumer",
        ),  # auto_approve=False, disable=True -> manual approval
    )
    @unpack
    def test_disable_autoapprove_overrides_auto_approval(
        self, auto_approve, disable_autoapprove, expected_state, mocked_task
    ):
        """Test that disable_autoapprove flag correctly overrides auto-approval logic."""
        consumer_fixture = provider_fixture = structure_fixtures.ProjectFixture()
        public_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            billable=True,
            customer=provider_fixture.customer,
            type="TEST_TYPE",
            plugin_options={
                "auto_approve_in_service_provider_projects": auto_approve,
                "disable_autoapprove": disable_autoapprove,
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
        self.assertEqual(response.data["state"], expected_state)

        # Verify notification task is called only when manual approval is required
        if expected_state == "pending-consumer":
            mocked_task.assert_called()
        else:
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
            state=OfferingStates.ACTIVE, options=OFFERING_OPTIONS
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
            state=OfferingStates.ACTIVE, type=SUPPORT_OFFERING
        )
        plan = factories.PlanFactory(offering=offering)

        for key in self.DEFAULT_LIMITS.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=BillingTypes.LIMIT,
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
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        for key in self.DEFAULT_LIMITS.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=BillingTypes.FIXED,
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
        LimitPeriods.TOTAL,
        LimitPeriods.MONTH,
        LimitPeriods.QUARTERLY,
        LimitPeriods.ANNUAL,
    )
    def test_offering_limit_is_valid(self, limit_period):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        models.OfferingComponent.objects.create(
            offering=offering,
            type="cpu_count",
            billing_type=BillingTypes.LIMIT,
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
        LimitPeriods.TOTAL,
        LimitPeriods.MONTH,
        LimitPeriods.QUARTERLY,
        LimitPeriods.ANNUAL,
    )
    def test_offering_limit_is_invalid(self, limit_period):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        models.OfferingComponent.objects.create(
            offering=offering,
            type="cpu_count",
            billing_type=BillingTypes.LIMIT,
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
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        models.OfferingTermsOfService.objects.create(
            offering=offering,
            terms_of_service="Terms of service",
            version="1.0",
            is_active=True,
        )
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
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        add_payload = {
            "offering": factories.OfferingFactory.get_public_url(offering),
            "attributes": {},
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    @override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
    def test_user_cannot_create_order_if_terms_of_service_have_been_not_accepted(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        models.OfferingTermsOfService.objects.create(
            offering=offering,
            terms_of_service="Terms of service",
            version="1.0",
            is_active=True,
        )
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


class OrderNameValidationTest(BaseOrderCreateTest):
    def test_order_creation_succeeds_with_valid_name_length(self):
        """Test that order creation succeeds when name is within valid length."""
        valid_name = "a" * (NAME_LENGTH - 1)  # 149 characters
        response = self.create_order(
            self.fixture.owner, add_payload={"attributes": {"name": valid_name}}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.name, valid_name)

    def test_order_creation_succeeds_with_maximum_name_length(self):
        """Test that order creation succeeds when name is exactly at maximum length."""
        max_length_name = "a" * NAME_LENGTH  # 150 characters
        response = self.create_order(
            self.fixture.owner, add_payload={"attributes": {"name": max_length_name}}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.name, max_length_name)

    def test_order_creation_fails_with_name_too_long(self):
        """Test that order creation fails when name exceeds maximum length."""
        too_long_name = "a" * (NAME_LENGTH + 1)  # 151 characters
        response = self.create_order(
            self.fixture.owner, add_payload={"attributes": {"name": too_long_name}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Name is too long", str(response.data))
        self.assertIn(str(NAME_LENGTH), str(response.data))

    def test_order_creation_succeeds_with_empty_name(self):
        """Test that order creation succeeds when name is empty."""
        response = self.create_order(
            self.fixture.owner, add_payload={"attributes": {"name": ""}}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.name, "")

    def test_order_creation_succeeds_without_name_attribute(self):
        """Test that order creation succeeds when name attribute is not provided."""
        response = self.create_order(self.fixture.owner, add_payload={"attributes": {}})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.name, "")

    def test_order_creation_with_unicode_name_within_limit(self):
        """Test that order creation succeeds with unicode characters within length limit."""
        unicode_name = "üõiõабвгдж" * 15  # Unicode characters, total length < 150
        response = self.create_order(
            self.fixture.owner, add_payload={"attributes": {"name": unicode_name}}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.name, unicode_name)

    def test_order_creation_fails_with_unicode_name_exceeding_limit(self):
        """Test that order creation fails with unicode characters exceeding length limit."""
        unicode_name = "üõiõабвгд" * 20  # Unicode characters, total length > 150
        response = self.create_order(
            self.fixture.owner, add_payload={"attributes": {"name": unicode_name}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Name is too long", str(response.data))


@override_constance_config(ENABLE_ORDER_START_DATE=True)
class OrderStartDateCreateTest(BaseOrderCreateTest):
    def test_order_creation_succeeds_with_valid_start_date(self):
        # Arrange
        future_date = (timezone.now() + datetime.timedelta(days=10)).date()
        payload = {"start_date": future_date.isoformat()}

        # Act
        response = self.create_order(self.fixture.owner, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.start_date, future_date)

    def test_order_creation_fails_with_past_start_date(self):
        # Arrange
        past_date = (timezone.now() - datetime.timedelta(days=1)).date()
        payload = {"start_date": past_date.isoformat()}

        # Act
        response = self.create_order(self.fixture.owner, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Start date cannot be in the past", str(response.data))

    def test_order_creation_fails_if_start_date_is_before_project_start_date(self):
        # Arrange
        self.project.start_date = (timezone.now() + datetime.timedelta(days=20)).date()
        self.project.save()

        order_start_date = (timezone.now() + datetime.timedelta(days=10)).date()
        payload = {"start_date": order_start_date.isoformat()}

        # Act
        response = self.create_order(self.fixture.owner, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Order start date cannot be earlier than the project start date",
            str(response.data),
        )

    def test_order_creation_fails_if_start_date_is_after_project_end_date(self):
        # Arrange
        self.project.end_date = (timezone.now() + datetime.timedelta(days=10)).date()
        self.project.save()

        order_start_date = (timezone.now() + datetime.timedelta(days=20)).date()
        payload = {"start_date": order_start_date.isoformat()}

        # Act
        response = self.create_order(self.fixture.owner, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Order start date cannot be later than the project end date",
            str(response.data),
        )

    def test_order_creation_succeeds_if_start_date_is_within_project_lifecycle(self):
        # Arrange
        self.project.start_date = (timezone.now() + datetime.timedelta(days=5)).date()
        self.project.end_date = (timezone.now() + datetime.timedelta(days=20)).date()
        self.project.save()

        order_start_date = (timezone.now() + datetime.timedelta(days=10)).date()
        payload = {"start_date": order_start_date.isoformat()}

        # Act
        response = self.create_order(self.fixture.owner, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.start_date, order_start_date)

    def test_order_creation_fails_with_invalid_date_format(self):
        # Arrange
        payload = {"start_date": "2024/01/15"}  # Invalid format

        # Act
        response = self.create_order(self.fixture.owner, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Date has wrong format", str(response.data))

    @override_constance_config(ENABLE_ORDER_START_DATE=False)
    def test_start_date_is_ignored_when_feature_is_disabled(self):
        # Arrange
        future_date = (timezone.now() + datetime.timedelta(days=10)).date()
        payload = {"start_date": future_date.isoformat()}

        # Act
        response = self.create_order(self.fixture.owner, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        # The serializer should have ignored the field, so it remains None in the model.
        self.assertIsNone(order.start_date)


@ddt
class OrderDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
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


class OrderUnlinkTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
        )

    def test_staff_can_unlink_order(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OrderFactory.get_url(self.order, action="unlink")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Order.objects.filter(pk=self.order.pk).exists())

    def test_non_staff_cannot_unlink_order(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OrderFactory.get_url(self.order, action="unlink")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class OrderFilterTest(test.APITestCase):
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
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)

        # Act
        self.client.force_authenticate(user)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.order.uuid.hex)

    def test_output_updated_at_is_visible_in_list_response(self):
        self.order.output = "Provisioning output"
        self.order.save(update_fields=["output"])

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIn("output_updated_at", response.data[0])
        self.assertIsNotNone(response.data[0]["output_updated_at"])


class OrderListNoDuplicatesTest(test.APITestCase):
    """Test that orders are not duplicated when a user has access
    through multiple permission paths (Fixes PUHURI-PORTALS-ETK)."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)

    def test_order_not_duplicated_when_user_is_both_consumer_and_provider(self):
        # Arrange: offering belongs to the same customer as the project,
        # so the owner matches both project__customer and offering__customer filters.
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        factories.OrderFactory(
            project=self.project,
            offering=offering,
            created_by=self.fixture.manager,
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url)

        # Assert: order should appear exactly once, not duplicated
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_order_not_duplicated_when_user_has_project_and_customer_roles(self):
        # Arrange: user is both project manager and customer owner,
        # matching both project__in and project__customer__in filters.
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        factories.OrderFactory(
            project=self.project,
            offering=offering,
            created_by=self.fixture.manager,
        )

        # The owner has access via project__customer (as customer owner)
        # The manager has access via project (as project manager)
        # But the owner also has access via project__customer, so just one path.
        # Let's make a user who is both manager and customer owner.
        user = self.fixture.manager
        self.fixture.customer.add_user(user, CustomerRole.OWNER)

        # Act
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url)

        # Assert: order should appear exactly once
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_order_not_duplicated_when_user_has_offering_role_and_customer_role(self):
        # Arrange: user is offering manager and also offering's customer owner,
        # matching both offering__in and offering__customer__in filters.
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        offering.add_user(self.fixture.owner, OfferingRole.MANAGER)

        factories.OrderFactory(
            project=structure_factories.ProjectFactory(),
            offering=offering,
            created_by=structure_factories.UserFactory(),
        )

        # Act: owner matches via offering__customer__in AND offering__in
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url)

        # Assert: order should appear exactly once
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@ddt
class OrderSetBackendIdTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE, customer=self.fixture.customer
        )
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            created_by=self.fixture.manager,
        )
        self.url = factories.OrderFactory.get_url(self.order, "set_backend_id")

        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_ID)

    def make_request(self, user, backend_id="new_backend_id"):
        self.client.force_authenticate(user)
        payload = {"backend_id": backend_id}
        return self.client.post(self.url, payload)

    @data("staff", "owner")
    def test_authorized_user_can_set_backend_id(self, user):
        """Test that service provider owners and staff can set backend_id."""
        user_obj = getattr(self.fixture, user)
        initial_backend_id = self.order.backend_id

        response = self.make_request(user_obj)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("status", response.data)
        self.assertEqual(response.data["status"], "Order backend_id has been changed.")
        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, "new_backend_id")
        self.assertNotEqual(self.order.backend_id, initial_backend_id)

    @data("admin", "manager")
    def test_unauthorized_user_cannot_set_backend_id(self, user):
        """Test that project-level users cannot set backend_id."""
        user_obj = getattr(self.fixture, user)

        response = self.make_request(user_obj)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_cannot_set_backend_id(self):
        """Test that unauthenticated users cannot set backend_id."""
        payload = {"backend_id": "new_backend_id"}
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_setting_same_backend_id_returns_not_changed_message(self):
        """Test that setting the same backend_id returns appropriate message."""
        self.order.backend_id = "existing_backend_id"
        self.order.save()

        response = self.make_request(self.fixture.staff, "existing_backend_id")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "Order backend_id is not changed.")
        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, "existing_backend_id")

    def test_setting_backend_id_to_none_fails(self):
        """Test that backend_id can be set to None."""
        self.order.backend_id = "existing_backend_id"
        self.order.save()

        response = self.make_request(self.fixture.staff, None)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_setting_backend_id_to_empty_string_fails(self):
        """Test that backend_id can be set to empty string."""
        self.order.backend_id = "existing_backend_id"
        self.order.save()

        response = self.make_request(self.fixture.staff, "")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_payload_returns_bad_request(self):
        """Test that invalid payload returns 400."""
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_order_not_found_returns_404(self):
        """Test that accessing non-existent order returns 404."""
        self.client.force_authenticate(self.fixture.staff)
        non_existent_order = factories.OrderFactory()
        url = factories.OrderFactory.get_url(non_existent_order, "set_backend_id")
        non_existent_order.delete()

        payload = {"backend_id": "new_backend_id"}
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_service_provider_owner_can_set_backend_id_for_different_customer_order(
        self,
    ):
        """Test that service provider owner can set backend_id for orders
        from different customers.
        """
        consumer_fixture = structure_fixtures.ProjectFixture()
        order = factories.OrderFactory(
            project=consumer_fixture.project,
            offering=self.offering,
            created_by=consumer_fixture.manager,
        )
        url = factories.OrderFactory.get_url(order, "set_backend_id")
        self.client.force_authenticate(self.fixture.owner)

        payload = {"backend_id": "cross_customer_backend_id"}
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.backend_id, "cross_customer_backend_id")

    def test_blocked_organization_cannot_set_backend_id(self):
        """Test that blocked organizations cannot set backend_id."""
        self.fixture.customer.blocked = True
        self.fixture.customer.save()

        response = self.make_request(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class OrderResourceActionTest(test.APITestCase):
    """Tests for the resource action that fetches connected resource via order."""

    def setUp(self):
        """Set up test fixtures."""
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.resource = self.fixture.resource

    @data("staff", "offering_owner", "manager", "admin")
    def test_authorized_user_can_fetch_connected_resource(self, user):
        """Test that authorized users can fetch connected resource via resource action."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)
        url = factories.OrderFactory.get_url(self.order, "resource")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(self.resource.uuid))

    def test_unrelated_user_cannot_fetch_connected_resource(self):
        """Test that unrelated user cannot fetch connected resource."""
        other_fixture = structure_fixtures.ProjectFixture()
        self.client.force_authenticate(other_fixture.user)
        url = factories.OrderFactory.get_url(self.order, "resource")
        response = self.client.get(url)

        # User should not have access to this order
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_resource_serializer_returns_all_fields(self):
        """Test that resource action returns properly serialized resource with all fields."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OrderFactory.get_url(self.order, "resource")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify key fields are present
        self.assertIn("uuid", response.data)
        self.assertIn("name", response.data)
        self.assertIn("state", response.data)
