from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, serializers
from waldur_mastermind.marketplace.enums import OfferingStates, OrderStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

SENIOR_ROLES = [RoleEnum.PROJECT_MANAGER, RoleEnum.PROJECT_ADMIN]


class RestrictedOfferingVisibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        # Materialize role assignments on the project.
        self.fixture.admin
        self.fixture.manager
        self.fixture.member
        self.restricted_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            billable=True,
            plugin_options={"restricted_to_roles": SENIOR_ROLES},
        )
        factories.PlanFactory(offering=self.restricted_offering)
        self.normal_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE, shared=True, billable=True
        )

    def list_offering_uuids(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.OfferingFactory.get_public_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {offering["uuid"] for offering in response.data}

    def test_member_does_not_see_restricted_offering(self):
        uuids = self.list_offering_uuids(self.fixture.member)
        self.assertNotIn(self.restricted_offering.uuid.hex, uuids)
        self.assertIn(self.normal_offering.uuid.hex, uuids)

    def test_manager_sees_restricted_offering(self):
        uuids = self.list_offering_uuids(self.fixture.manager)
        self.assertIn(self.restricted_offering.uuid.hex, uuids)

    def test_admin_sees_restricted_offering(self):
        uuids = self.list_offering_uuids(self.fixture.admin)
        self.assertIn(self.restricted_offering.uuid.hex, uuids)

    def test_staff_sees_restricted_offering(self):
        uuids = self.list_offering_uuids(self.fixture.staff)
        self.assertIn(self.restricted_offering.uuid.hex, uuids)

    def test_non_member_does_not_see_restricted_offering(self):
        uuids = self.list_offering_uuids(self.fixture.user)
        self.assertNotIn(self.restricted_offering.uuid.hex, uuids)

    def test_existing_resource_owner_still_sees_restricted_offering(self):
        # A member who already consumes a resource from the offering keeps
        # catalog access even after it becomes restricted.
        factories.ResourceFactory(
            project=self.fixture.project, offering=self.restricted_offering
        )
        uuids = self.list_offering_uuids(self.fixture.member)
        self.assertIn(self.restricted_offering.uuid.hex, uuids)

    def test_member_cannot_retrieve_restricted_offering(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.get(
            factories.OfferingFactory.get_public_url(self.restricted_offering)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_manager_can_retrieve_restricted_offering(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(
            factories.OfferingFactory.get_public_url(self.restricted_offering)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_accessible"])


class RestrictedOfferingOrderTest(BaseOrderCreateTest):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.restricted_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            billable=True,
            plugin_options={"restricted_to_roles": SENIOR_ROLES},
        )

    def test_member_cannot_order_restricted_offering(self):
        response = self.create_order(self.fixture.member, self.restricted_offering)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_order_restricted_offering(self):
        response = self.create_order(self.fixture.manager, self.restricted_offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_admin_can_order_restricted_offering(self):
        response = self.create_order(self.fixture.admin, self.restricted_offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_can_order_restricted_offering(self):
        response = self.create_order(self.fixture.staff, self.restricted_offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_restriction_alone_does_not_skip_consumer_review(self):
        # A restricted-role holder WITHOUT ORDER.APPROVE still needs consumer
        # review: the restriction governs visibility/ordering, not approval.
        response = self.create_order(self.fixture.manager, self.restricted_offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.state, OrderStates.PENDING_CONSUMER)

    def test_consumer_review_skipped_when_role_has_approve_permission(self):
        # Approval-skip is driven by ORDER.APPROVE on the ordering user's role,
        # exactly as for any other offering.
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        response = self.create_order(self.fixture.manager, self.restricted_offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertNotEqual(order.state, OrderStates.PENDING_CONSUMER)

    def test_manager_order_on_normal_shared_offering_requires_review(self):
        normal_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE, shared=True, billable=True
        )
        response = self.create_order(self.fixture.manager, normal_offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.state, OrderStates.PENDING_CONSUMER)

    def test_manager_in_other_project_cannot_order_into_member_project(self):
        # User is MANAGER in another project but only MEMBER in self.project.
        other_fixture = structure_fixtures.ProjectFixture()
        user = self.fixture.member  # MEMBER in self.project (the order target)
        other_fixture.project.add_user(user, ProjectRole.MANAGER)
        response = self.create_order(user, self.restricted_offering)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RestrictedOfferingValidationTest(test.APITestCase):
    def test_invalid_role_name_is_rejected(self):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"restricted_to_roles": ["NONEXISTENT.ROLE"]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("restricted_to_roles", serializer.errors)

    def test_valid_role_names_are_accepted(self):
        ProjectRole.MANAGER  # ensure the role exists in the DB
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"restricted_to_roles": [ProjectRole.MANAGER.name]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_customer_role_is_accepted(self):
        CustomerRole.OWNER  # ensure the role exists in the DB
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"restricted_to_roles": [CustomerRole.OWNER.name]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_non_project_or_customer_role_is_rejected(self):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"restricted_to_roles": [OfferingRole.MANAGER.name]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("restricted_to_roles", serializer.errors)

    def test_empty_list_is_accepted(self):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"restricted_to_roles": []}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
