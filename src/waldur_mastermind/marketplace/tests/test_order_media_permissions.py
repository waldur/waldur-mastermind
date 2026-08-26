from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.managers import filter_orders_for_user
from waldur_mastermind.marketplace.tests import factories, fixtures


class FilterOrdersForUserTest(TestCase):
    """The predicate shared by OrderViewSet and the order attachment media rule."""

    def setUp(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order

    def can_list(self, user):
        return filter_orders_for_user(
            models.Order.objects.filter(pk=self.order.pk), user
        ).exists()

    def test_staff_can_list_order(self):
        self.assertTrue(self.can_list(self.fixture.staff))

    def test_support_can_list_order(self):
        self.assertTrue(self.can_list(self.fixture.global_support))

    def test_consumer_owner_can_list_order(self):
        self.assertTrue(self.can_list(self.fixture.owner))

    def test_provider_offering_manager_can_list_order(self):
        self.assertTrue(self.can_list(self.fixture.offering_manager))

    def test_unrelated_user_cannot_list_order(self):
        self.assertFalse(self.can_list(structure_factories.UserFactory()))

    def test_anonymous_user_cannot_list_order(self):
        self.assertFalse(self.can_list(AnonymousUser()))

    def test_user_with_project_membership_but_without_list_orders(self):
        member = structure_factories.UserFactory()
        self.fixture.project.add_user(member, ProjectRole.MEMBER)
        ProjectRole.MEMBER.permissions.filter(
            permission=PermissionEnum.LIST_ORDERS
        ).delete()
        self.assertFalse(self.can_list(member))

    def test_order_from_other_project_is_not_listable(self):
        other_order = factories.OrderFactory()
        self.assertFalse(
            filter_orders_for_user(
                models.Order.objects.filter(pk=other_order.pk), self.fixture.owner
            ).exists()
        )
