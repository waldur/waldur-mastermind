"""List visibility must follow the grant's own role permissions, as has_permission does.

The permission-based scope helpers used to resolve a permission to role names
and then match grants by role name or by their template's name. That disagreed
with has_permission for clones narrowed by staff, for clones that drifted behind
their template, and for disabled roles. These tests pin down the agreement.
"""

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role
from waldur_core.permissions.serializers import clone_role_for_customer
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers_by_permission,
    get_connected_projects_by_permission,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.managers import (
    get_connected_offerings_by_permission,
)
from waldur_mastermind.marketplace.tests import fixtures


class OrderListByPermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.order = self.fixture.order
        self.template = Role.objects.create(
            name="CUSTOMER.ORDER_VIEWER",
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
        )

    def grant(self, role):
        user = structure_factories.UserFactory()
        self.customer.add_user(user, role)
        return user

    def listed_orders(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(reverse("marketplace-order-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def test_role_holding_the_permission_lists_orders(self):
        # Control case: both checks agree.
        self.template.add_permission(PermissionEnum.LIST_ORDERS)
        user = self.grant(self.template)
        self.assertTrue(has_permission(user, PermissionEnum.LIST_ORDERS, self.customer))
        self.assertIn(self.order.uuid.hex, self.listed_orders(user))

    def test_clone_narrowed_by_staff_does_not_list_orders(self):
        # Security case: staff removed the permission from the clone only.
        self.template.add_permission(PermissionEnum.LIST_ORDERS)
        clone = clone_role_for_customer(self.template, self.customer)
        clone.delete_permission(PermissionEnum.LIST_ORDERS)
        user = self.grant(clone)
        self.assertFalse(
            has_permission(user, PermissionEnum.LIST_ORDERS, self.customer)
        )
        self.assertNotIn(self.order.uuid.hex, self.listed_orders(user))

    def test_clone_made_before_template_gained_permission(self):
        # Compatibility case: clones copy the template's permissions once and
        # import_roles does not update them, so a permission added to the
        # template later is missing on the clone. Name matching let the
        # clone's holders list orders anyway; matching on the grant's own
        # permissions takes that away, as has_permission already does.
        clone = clone_role_for_customer(self.template, self.customer)
        self.template.add_permission(PermissionEnum.LIST_ORDERS)
        user = self.grant(clone)
        self.assertFalse(
            has_permission(user, PermissionEnum.LIST_ORDERS, self.customer)
        )
        self.assertNotIn(self.order.uuid.hex, self.listed_orders(user))

    def test_disabled_role_keeps_existing_holders_listing_orders(self):
        # Disabling a role stops new assignments; existing ones are documented
        # as unaffected, and has_permission still grants the permission.
        self.template.add_permission(PermissionEnum.LIST_ORDERS)
        user = self.grant(self.template)
        self.template.is_active = False
        self.template.save()
        self.assertTrue(has_permission(user, PermissionEnum.LIST_ORDERS, self.customer))
        self.assertIn(self.order.uuid.hex, self.listed_orders(user))


class ScopeHelperAgreementMixin:
    """Each helper must list exactly the scopes has_permission allows."""

    permission = PermissionEnum.LIST_ORDERS

    def assertAgrees(self, user, expected):
        self.assertEqual(has_permission(user, self.permission, self.scope), expected)
        self.assertEqual(
            self.scope.id in set(self.helper(user, self.permission)), expected
        )


class CustomerHelperTest(ScopeHelperAgreementMixin, test.APITestCase):
    helper = staticmethod(get_connected_customers_by_permission)

    def setUp(self):
        self.scope = structure_factories.CustomerFactory()
        self.template = Role.objects.create(
            name="CUSTOMER.SCOPE_HELPER",
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
        )
        self.template.add_permission(self.permission)
        self.user = structure_factories.UserFactory()

    def test_role_holding_the_permission(self):
        self.scope.add_user(self.user, self.template)
        self.assertAgrees(self.user, True)

    def test_narrowed_clone(self):
        clone = clone_role_for_customer(self.template, self.scope)
        clone.delete_permission(self.permission)
        self.scope.add_user(self.user, clone)
        self.assertAgrees(self.user, False)

    def test_drifted_clone(self):
        # The clone predates the template gaining the permission. Its holders
        # no longer list through the template's name, matching has_permission.
        self.template.delete_permission(self.permission)
        clone = clone_role_for_customer(self.template, self.scope)
        self.template.add_permission(self.permission)
        self.scope.add_user(self.user, clone)
        self.assertAgrees(self.user, False)

    def test_disabled_role(self):
        self.scope.add_user(self.user, self.template)
        self.template.is_active = False
        self.template.save()
        self.assertAgrees(self.user, True)

    def test_role_on_another_customer(self):
        structure_factories.CustomerFactory().add_user(self.user, self.template)
        self.assertAgrees(self.user, False)


class ProjectHelperTest(ScopeHelperAgreementMixin, test.APITestCase):
    helper = staticmethod(get_connected_projects_by_permission)

    def setUp(self):
        self.scope = structure_factories.ProjectFactory()
        self.template = Role.objects.create(
            name="PROJECT.SCOPE_HELPER",
            content_type=ContentType.objects.get_for_model(structure_models.Project),
        )
        self.template.add_permission(self.permission)
        self.user = structure_factories.UserFactory()

    def test_role_holding_the_permission(self):
        self.scope.add_user(self.user, self.template)
        self.assertAgrees(self.user, True)

    def test_narrowed_clone(self):
        clone = clone_role_for_customer(self.template, self.scope.customer)
        clone.delete_permission(self.permission)
        self.scope.add_user(self.user, clone)
        self.assertAgrees(self.user, False)

    def test_drifted_clone(self):
        self.template.delete_permission(self.permission)
        clone = clone_role_for_customer(self.template, self.scope.customer)
        self.template.add_permission(self.permission)
        self.scope.add_user(self.user, clone)
        self.assertAgrees(self.user, False)

    def test_disabled_role(self):
        self.scope.add_user(self.user, self.template)
        self.template.is_active = False
        self.template.save()
        self.assertAgrees(self.user, True)


class OfferingHelperTest(ScopeHelperAgreementMixin, test.APITestCase):
    helper = staticmethod(get_connected_offerings_by_permission)

    def setUp(self):
        self.scope = fixtures.MarketplaceFixture().offering
        self.role = Role.objects.create(
            name="OFFERING.SCOPE_HELPER",
            content_type=ContentType.objects.get_for_model(models.Offering),
        )
        self.role.add_permission(self.permission)
        self.user = structure_factories.UserFactory()

    def test_role_holding_the_permission(self):
        self.scope.add_user(self.user, self.role)
        self.assertAgrees(self.user, True)

    def test_role_without_the_permission(self):
        self.role.delete_permission(self.permission)
        self.scope.add_user(self.user, self.role)
        self.assertAgrees(self.user, False)

    def test_disabled_role(self):
        self.scope.add_user(self.user, self.role)
        self.role.is_active = False
        self.role.save()
        self.assertAgrees(self.user, True)
