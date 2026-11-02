"""Writes reserved to staff in the structure app. A staff or support user's
personal access token without the matching scope acts as the user without that
role (#980).
"""

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import models as core_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.permissions.utils import get_permissions
from waldur_core.structure.tests import factories, fixtures


@override_config(PAT_ENABLED=True)
class SshKeyDeletionTest(test.APITestCase):
    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.key = factories.SshPublicKeyFactory()
        self.url = factories.SshPublicKeyFactory.get_url(self.key)

    def assert_deleted(self, deleted):
        response = self.client.delete(self.url)
        if deleted:
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        else:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            core_models.SshPublicKey.objects.filter(pk=self.key.pk).exists(),
            not deleted,
        )

    def test_staff_session_can_delete_another_users_key(self):
        self.client.force_authenticate(self.staff)
        self.assert_deleted(True)

    def test_staff_token_with_staff_scope_can_delete_another_users_key(self):
        use_pat(self.client, self.staff, STAFF)
        self.assert_deleted(True)

    def test_staff_token_without_staff_scope_cannot_delete_another_users_key(self):
        use_pat(self.client, self.staff)
        self.assert_deleted(False)

    def test_support_token_without_support_scope_cannot_delete_another_users_key(
        self,
    ):
        use_pat(self.client, self.support)
        self.assert_deleted(False)

    def test_support_token_with_support_scope_cannot_delete_another_users_key(
        self,
    ):
        use_pat(self.client, self.support, SUPPORT)
        self.assert_deleted(False)

    def test_support_session_cannot_delete_another_users_key(self):
        # Support is read-only.
        self.client.force_authenticate(self.support)
        self.assert_deleted(False)

    def test_support_session_can_read_another_users_key(self):
        self.client.force_authenticate(self.support)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    def test_support_session_deletes_its_own_key(self):
        own_key = factories.SshPublicKeyFactory(user=self.support)
        self.client.force_authenticate(self.support)
        response = self.client.delete(factories.SshPublicKeyFactory.get_url(own_key))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_other_user_cannot_delete_the_key(self):
        self.client.force_authenticate(factories.UserFactory())
        self.assert_deleted(False)

    def test_token_still_deletes_its_own_users_key(self):
        own_key = factories.SshPublicKeyFactory(user=self.staff)
        use_pat(self.client, self.staff)
        response = self.client.delete(factories.SshPublicKeyFactory.get_url(own_key))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


@override_config(PAT_ENABLED=True)
class CustomerStaffOnlyFieldsTest(test.APITestCase):
    """`PATCH /api/customers/<uuid>/` takes the staff-only fields from staff
    only. The token carries `CUSTOMER.UPDATE`, which the update itself needs."""

    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.staff = factories.UserFactory(is_staff=True)
        self.url = factories.CustomerFactory.get_url(self.customer)

    def patch(self):
        response = self.client.patch(self.url, {"agreement_number": "AGR-42"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.customer.refresh_from_db()
        return self.customer.agreement_number

    def test_staff_session_sets_staff_only_field(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.patch(), "AGR-42")

    def test_staff_token_with_staff_scope_sets_staff_only_field(self):
        use_pat(self.client, self.staff, STAFF, PermissionEnum.UPDATE_CUSTOMER.value)
        self.assertEqual(self.patch(), "AGR-42")

    def test_staff_token_without_staff_scope_cannot_set_staff_only_field(self):
        use_pat(self.client, self.staff, PermissionEnum.UPDATE_CUSTOMER.value)
        self.assertEqual(self.patch(), "")

    def test_owner_cannot_set_staff_only_field(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.patch(), "")


@override_config(PAT_ENABLED=True)
class ProjectRecoveryTokenScopeTest(test.APITestCase):
    """Restoring a recovered project's team is reserved to staff."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff = factories.UserFactory(is_staff=True)
        self.project.add_user(factories.UserFactory(), ProjectRole.ADMIN)
        self.project.delete()
        self.url = factories.ProjectFactory.get_url(self.project, action="recover")

    def recover(self):
        return self.client.post(self.url, {"restore_team_members": True})

    def assert_restored(self, response, restored):
        if restored:
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        else:
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(get_permissions(self.project).count(), 1 if restored else 0)

    def test_staff_session_restores_the_team(self):
        self.client.force_authenticate(self.staff)
        self.assert_restored(self.recover(), True)

    def test_staff_token_with_staff_scope_restores_the_team(self):
        use_pat(self.client, self.staff, STAFF, PermissionEnum.CREATE_PROJECT.value)
        self.assert_restored(self.recover(), True)

    def test_staff_token_without_staff_scope_cannot_restore_the_team(self):
        use_pat(self.client, self.staff, PermissionEnum.CREATE_PROJECT.value)
        self.assert_restored(self.recover(), False)

    def test_owner_cannot_restore_the_team(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        self.client.force_authenticate(self.fixture.owner)
        self.assert_restored(self.recover(), False)
