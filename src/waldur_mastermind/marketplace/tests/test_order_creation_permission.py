"""Order-minting resource actions must also carry order creation rights.

Changing limits, switching a plan or renewing a resource does not merely mutate
the resource — each submits a marketplace order. These tests pin that such an
action needs CREATE_ORDER on top of its own resource permission, so a role that
was granted only the resource permission cannot mint orders through it.
"""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OfferingStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


class LimitActionPermissionMixin:
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # The fixture already ships one component on the offering; make it a
        # limit component so the resource has something to update.
        self.component = self.fixture.offering_component
        self.component.billing_type = BillingTypes.LIMIT
        self.component.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.limits = {self.component.type: 1}
        self.resource.save()

    def grant(self, *permissions):
        for permission in permissions:
            CustomerRole.OWNER.add_permission(permission)
            self.addCleanup(
                lambda permission=permission: CustomerRole.OWNER.delete_permission(
                    permission
                )
            )

    def update_limits(self, user=None):
        self.client.force_authenticate(user or self.fixture.owner)
        url = factories.ResourceFactory.get_url(self.resource, "update_limits")
        return self.client.post(url, {"limits": {self.component.type: 10}})


@ddt
class UpdateLimitsOrderPermissionTest(LimitActionPermissionMixin, test.APITestCase):
    def test_limits_permission_alone_is_not_enough(self):
        self.grant(PermissionEnum.UPDATE_RESOURCE_LIMITS)

        response = self.update_limits()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).exists()
        )

    def test_order_permission_alone_is_not_enough(self):
        self.grant(PermissionEnum.CREATE_ORDER)

        response = self.update_limits()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_both_permissions_allow_the_update(self):
        self.grant(PermissionEnum.UPDATE_RESOURCE_LIMITS, PermissionEnum.CREATE_ORDER)

        response = self.update_limits()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).exists()
        )

    def test_staff_is_not_affected(self):
        response = self.update_limits(self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("renew", "switch_plan")
    def test_other_order_minting_actions_reject_limits_permission_alone(self, action):
        """The whole update family is gated, not just update_limits.

        Only the 403 is asserted here: each action has its own payload and
        state preconditions, and what matters is that the permission check
        fires before any of that is reached.
        """
        self.grant(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
            PermissionEnum.SWITCH_RESOURCE_PLAN,
        )
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_url(self.resource, action)

        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RestrictedOfferingLimitChangeTest(LimitActionPermissionMixin, test.APITestCase):
    """An offering restricted to designated roles cannot be grown by editing an
    existing resource instead of ordering a new one."""

    def test_user_outside_the_restricted_roles_is_denied(self):
        self.grant(PermissionEnum.UPDATE_RESOURCE_LIMITS, PermissionEnum.CREATE_ORDER)
        self.resource.offering.plugin_options = {
            "restricted_to_roles": ["PROJECT.ADMIN"]
        }
        self.resource.offering.save()

        response = self.update_limits()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_holding_a_restricted_role_is_allowed(self):
        self.grant(PermissionEnum.UPDATE_RESOURCE_LIMITS, PermissionEnum.CREATE_ORDER)
        self.resource.offering.plugin_options = {
            "restricted_to_roles": ["CUSTOMER.OWNER"]
        }
        self.resource.offering.save()

        response = self.update_limits()

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unrestricted_offering_is_unaffected(self):
        self.grant(PermissionEnum.UPDATE_RESOURCE_LIMITS, PermissionEnum.CREATE_ORDER)

        response = self.update_limits()

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class OrderCreateTypeTest(test.APITestCase):
    """The order creation endpoint always provisions a new resource, so the
    order it produces is always a CREATE regardless of what the caller sends."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

    def test_caller_supplied_type_is_ignored(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "offering": factories.OfferingFactory.get_public_url(self.offering),
            "project": structure_factories.ProjectFactory.get_url(self.fixture.project),
            "attributes": {},
            "plan": factories.PlanFactory.get_public_url(self.fixture.plan),
            "type": "Terminate",
        }

        response = self.client.post(factories.OrderFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.type, OrderTypes.CREATE)
