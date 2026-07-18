from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, serializers
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    OfferingStates,
    OrderStates,
)
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

    def list_offering_uuids(self, user, accessible=None):
        self.client.force_authenticate(user)
        params = {} if accessible is None else {"accessible": accessible}
        response = self.client.get(
            factories.OfferingFactory.get_public_list_url(), params
        )
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
        # catalog access even after it becomes restricted (default behaviour).
        factories.ResourceFactory(
            project=self.fixture.project, offering=self.restricted_offering
        )
        uuids = self.list_offering_uuids(self.fixture.member)
        self.assertIn(self.restricted_offering.uuid.hex, uuids)

    def test_accessible_filter_hides_restricted_offering_from_member(self):
        # With accessible=true the catalog only returns orderable offerings.
        uuids = self.list_offering_uuids(self.fixture.member, accessible=True)
        self.assertNotIn(self.restricted_offering.uuid.hex, uuids)
        self.assertIn(self.normal_offering.uuid.hex, uuids)

    def test_accessible_filter_hides_consumed_restricted_offering(self):
        # Even a member whose project already consumes a resource from the
        # restricted offering does not see it when accessible=true: they still
        # cannot order a new one.
        factories.ResourceFactory(
            project=self.fixture.project, offering=self.restricted_offering
        )
        uuids = self.list_offering_uuids(self.fixture.member, accessible=True)
        self.assertNotIn(self.restricted_offering.uuid.hex, uuids)

    def test_accessible_filter_keeps_restricted_offering_for_manager(self):
        # A user holding a required role can order it, so accessible=true keeps
        # it visible.
        uuids = self.list_offering_uuids(self.fixture.manager, accessible=True)
        self.assertIn(self.restricted_offering.uuid.hex, uuids)

    def test_accessible_false_does_not_filter(self):
        # accessible=false is a no-op: default catalog behaviour applies.
        factories.ResourceFactory(
            project=self.fixture.project, offering=self.restricted_offering
        )
        uuids = self.list_offering_uuids(self.fixture.member, accessible=False)
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


class RestrictedOfferingCategoryCountTest(test.APITestCase):
    """The `accessible` filter on the categories endpoint must keep category
    offering_count (and the category list itself) in sync with the offerings
    catalog, so the "Add resource" quick-add does not surface restricted
    offerings the user cannot order."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.fixture.manager
        self.fixture.member
        # Mixed category: one restricted + one normal offering. It survives the
        # accessible category filter (it has an orderable offering), so its
        # offering_count can be inspected on retrieve.
        self.mixed_category = factories.CategoryFactory()
        self.restricted_offering = factories.OfferingFactory(
            category=self.mixed_category,
            state=OfferingStates.ACTIVE,
            shared=True,
            billable=True,
            plugin_options={"restricted_to_roles": SENIOR_ROLES},
        )
        factories.PlanFactory(offering=self.restricted_offering)
        factories.OfferingFactory(
            category=self.mixed_category,
            state=OfferingStates.ACTIVE,
            shared=True,
            billable=True,
        )
        # Restricted-only category: no orderable offering for the member.
        self.restricted_only_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            billable=True,
            plugin_options={"restricted_to_roles": SENIOR_ROLES},
        )
        factories.PlanFactory(offering=self.restricted_only_offering)
        self.restricted_only_category = self.restricted_only_offering.category
        # The member's project already consumes both restricted offerings, so
        # the default (carve-out) queryset keeps counting them.
        factories.ResourceFactory(
            project=self.fixture.project, offering=self.restricted_offering
        )
        factories.ResourceFactory(
            project=self.fixture.project, offering=self.restricted_only_offering
        )

    def get_offering_count(self, category, user, accessible=None):
        self.client.force_authenticate(user)
        params = {"field": ["uuid", "offering_count"]}
        if accessible is not None:
            params["accessible"] = accessible
        response = self.client.get(factories.CategoryFactory.get_url(category), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["offering_count"]

    def list_category_uuids(self, user, accessible=None):
        self.client.force_authenticate(user)
        params = {} if accessible is None else {"accessible": accessible}
        response = self.client.get(factories.CategoryFactory.get_list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {category["uuid"] for category in response.data}

    def test_member_count_includes_consumed_restricted_offering(self):
        # Default behaviour: the carve-out keeps the consumed offering counted.
        self.assertEqual(
            self.get_offering_count(self.mixed_category, self.fixture.member), 2
        )

    def test_accessible_count_excludes_restricted_offering_for_member(self):
        # Only the normal offering remains orderable for the member.
        self.assertEqual(
            self.get_offering_count(
                self.mixed_category, self.fixture.member, accessible=True
            ),
            1,
        )

    def test_accessible_count_keeps_restricted_offering_for_manager(self):
        self.assertEqual(
            self.get_offering_count(
                self.mixed_category, self.fixture.manager, accessible=True
            ),
            2,
        )

    def test_accessible_filter_drops_restricted_only_category_for_member(self):
        uuids = self.list_category_uuids(self.fixture.member, accessible=True)
        self.assertNotIn(self.restricted_only_category.uuid.hex, uuids)
        self.assertIn(self.mixed_category.uuid.hex, uuids)

    def test_accessible_filter_keeps_restricted_only_category_for_manager(self):
        uuids = self.list_category_uuids(self.fixture.manager, accessible=True)
        self.assertIn(self.restricted_only_category.uuid.hex, uuids)

    def test_category_list_unfiltered_keeps_consumed_category_for_member(self):
        uuids = self.list_category_uuids(self.fixture.member)
        self.assertIn(self.restricted_only_category.uuid.hex, uuids)


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


class AutoApproveForRolesOrderTest(BaseOrderCreateTest):
    """auto_approve_for_roles skips consumer review for orders created by the
    designated roles, independently of the ORDER.APPROVE permission and of
    restricted_to_roles."""

    def setUp(self):
        super().setUp()
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            billable=True,
            plugin_options={"auto_approve_for_roles": [RoleEnum.PROJECT_MANAGER]},
        )

    def _order(self, response):
        return models.Order.objects.get(uuid=response.data["uuid"])

    def test_designated_role_skips_consumer_review_without_approve_permission(self):
        # Manager holds no ORDER.APPROVE permission, yet the offering designates
        # PROJECT.MANAGER for auto-approval.
        response = self.create_order(self.fixture.manager, self.offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(self._order(response).state, OrderStates.PENDING_CONSUMER)

    def test_non_designated_role_still_reviewed(self):
        response = self.create_order(self.fixture.member, self.offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._order(response).state, OrderStates.PENDING_CONSUMER)

    def test_designated_role_in_other_project_does_not_skip(self):
        # User is MANAGER in an unrelated project but only MEMBER in the target
        # project: the scope check must not auto-approve.
        other_fixture = structure_fixtures.ProjectFixture()
        user = self.fixture.member
        other_fixture.project.add_user(user, ProjectRole.MANAGER)
        response = self.create_order(user, self.offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._order(response).state, OrderStates.PENDING_CONSUMER)

    def test_purchase_order_requirement_still_blocks_auto_approval(self):
        self.offering.plugin_options["require_purchase_order_upload"] = True
        self.offering.save()
        response = self.create_order(self.fixture.manager, self.offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._order(response).state, OrderStates.PENDING_CONSUMER)

    def test_normal_offering_without_option_is_unaffected(self):
        normal_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE, shared=True, billable=True
        )
        response = self.create_order(self.fixture.manager, normal_offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._order(response).state, OrderStates.PENDING_CONSUMER)


class AutoApproveForRolesValidationTest(test.APITestCase):
    def test_invalid_role_name_is_rejected(self):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"auto_approve_for_roles": ["NONEXISTENT.ROLE"]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("auto_approve_for_roles", serializer.errors)

    def test_valid_project_role_is_accepted(self):
        ProjectRole.MANAGER  # ensure the role exists in the DB
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"auto_approve_for_roles": [ProjectRole.MANAGER.name]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_non_project_or_customer_role_is_rejected(self):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"auto_approve_for_roles": [OfferingRole.MANAGER.name]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("auto_approve_for_roles", serializer.errors)


class AutoApproveForRolesStaffOnlyTest(test.APITestCase):
    """Only staff may change auto_approve_for_roles via update_integration."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        ProjectRole.MANAGER  # materialize the role so name validation passes
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION
        )
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)

    def _update(self, user, roles):
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(
            url, {"plugin_options": {"auto_approve_for_roles": roles}}
        )

    def test_staff_can_set_auto_approve_for_roles(self):
        response = self._update(self.fixture.staff, [RoleEnum.PROJECT_MANAGER])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["auto_approve_for_roles"],
            [RoleEnum.PROJECT_MANAGER],
        )

    def test_owner_cannot_set_auto_approve_for_roles(self):
        response = self._update(self.fixture.owner, [RoleEnum.PROJECT_MANAGER])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plugin_options", response.data)

    def test_owner_unchanged_value_is_accepted(self):
        # Submitting the same value is not a change and must not be blocked.
        self.offering.plugin_options["auto_approve_for_roles"] = [
            RoleEnum.PROJECT_MANAGER
        ]
        self.offering.save()
        response = self._update(self.fixture.owner, [RoleEnum.PROJECT_MANAGER])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class AutoApproveForRolesCreateStaffOnlyTest(test.APITestCase):
    """Only staff may seed auto_approve_for_roles when creating an offering."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        ProjectRole.MANAGER  # materialize the role so name validation passes
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)

    def _create(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(
            factories.OfferingFactory.get_list_url(),
            {
                "name": "Restricted approval offering",
                "category": factories.CategoryFactory.get_url(),
                "customer": structure_factories.CustomerFactory.get_url(self.customer),
                "type": SUPPORT_OFFERING,
                "plans": [{"name": "Small"}],
                "plugin_options": {
                    "auto_approve_for_roles": [RoleEnum.PROJECT_MANAGER]
                },
            },
            format="json",
        )

    def test_owner_cannot_seed_auto_approve_for_roles_on_create(self):
        response = self._create("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plugin_options", response.data)

    def test_staff_can_seed_auto_approve_for_roles_on_create(self):
        response = self._create("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.plugin_options["auto_approve_for_roles"],
            [RoleEnum.PROJECT_MANAGER],
        )
