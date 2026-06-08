from rest_framework import test

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class OfferingDeletionRoleRevocationTest(test.APITestCase):
    """Deleting an offering must revoke its scoped roles, otherwise they are
    left active with an unresolvable scope (the orphaned-role bug)."""

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.user = UserFactory()
        self.offering.add_user(self.user, OfferingRole.MANAGER)

    def _manager_roles(self, **kwargs):
        return UserRole.objects.filter(
            user=self.user, role__name=RoleEnum.OFFERING_MANAGER, **kwargs
        )

    def test_offering_manager_role_is_active_before_deletion(self):
        self.assertTrue(self._manager_roles(is_active=True).exists())

    def test_offering_manager_role_is_revoked_when_offering_is_deleted(self):
        role = self._manager_roles(is_active=True).get()

        self.offering.delete()

        role.refresh_from_db()
        self.assertFalse(role.is_active)

    def test_no_active_orphan_role_remains_after_deletion(self):
        self.offering.delete()

        self.assertFalse(self._manager_roles(is_active=True).exists())

    def test_unrelated_offering_role_is_not_revoked(self):
        other_offering = marketplace_factories.OfferingFactory()
        other_offering.add_user(self.user, OfferingRole.MANAGER)

        self.offering.delete()

        self.assertTrue(
            UserRole.objects.filter(
                user=self.user,
                object_id=other_offering.id,
                is_active=True,
            ).exists()
        )
