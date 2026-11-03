from types import SimpleNamespace

from rest_framework import exceptions, status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import access
from waldur_core.core import models as core_models
from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure import permissions, views
from waldur_core.structure.tests import factories


class BaseSshKeyTest(test.APITestCase):
    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.user = factories.UserFactory()
        self.user_key = factories.SshPublicKeyFactory(user=self.user)


class SshKeyRetrieveListTest(BaseSshKeyTest):
    def test_staff_can_get_any_key(self):
        self.client.force_authenticate(self.staff)
        url = factories.SshPublicKeyFactory.get_url(self.user_key)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_can_get_own_key(self):
        self.client.force_authenticate(self.user)
        url = factories.SshPublicKeyFactory.get_url(self.user_key)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_can_not_get_other_key(self):
        self.client.force_authenticate(self.user)
        shared_key = factories.SshPublicKeyFactory(user=self.staff)
        url = factories.SshPublicKeyFactory.get_url(shared_key)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_any_user_can_get_shared_key(self):
        self.client.force_authenticate(self.user)
        shared_key = factories.SshPublicKeyFactory(user=self.staff, is_shared=True)
        url = factories.SshPublicKeyFactory.get_url(shared_key)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_get_type_of_key(self):
        self.client.force_authenticate(self.staff)
        url = factories.SshPublicKeyFactory.get_url(self.user_key)
        response = self.client.get(url)
        self.assertEqual(response.data["type"], "ssh-rsa")


class SshKeyCreateTest(BaseSshKeyTest):
    def test_key_user_and_name_uniqueness(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {
            "name": self.user_key.name,
            "public_key": key.public_key,
        }

        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)
        self.assertEqual(response.data["name"], ["This field must be unique."])

    def test_valid_key_creation(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {
            "name": key.name,
            "public_key": key.public_key,
        }
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.assertTrue(
            core_models.SshPublicKey.objects.filter(name=data["name"]).exists(),
            "New key should have been created in the database",
        )

    def test_key_name_is_stripped(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {
            "name": "  " + key.name + "  ",
            "public_key": key.public_key,
        }
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(core_models.SshPublicKey.objects.filter(name=key.name).exists())

    def test_user_can_add_ssh_key_with_duplicate_fingerprint(self):
        staff = factories.UserFactory(is_staff=True)
        key = factories.SshPublicKeyFactory()
        data = {
            "name": "test",
            "public_key": key.public_key,
        }

        self.client.force_authenticate(staff)
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(core_models.SshPublicKey.objects.filter(**data).exists())

    def test_user_cannot_add_ssh_key_with_new_lines(self):
        staff = factories.UserFactory(is_staff=True)
        key = factories.SshPublicKeyFactory.build()
        data = {
            "name": "test",
            "public_key": key.public_key + "\nABCD",
        }

        self.client.force_authenticate(staff)
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(core_models.SshPublicKey.objects.filter(**data).exists())


class SshKeyDeleteTest(BaseSshKeyTest):
    def test_staff_user_can_delete_any_key(self):
        self.client.force_authenticate(self.staff)
        response = self.client.delete(
            factories.SshPublicKeyFactory.get_url(self.user_key)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_user_can_delete_his_key(self):
        self.client.force_authenticate(self.user)
        response = self.client.delete(
            factories.SshPublicKeyFactory.get_url(self.user_key)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_user_cannot_delete_other_users_key(self):
        other_key = factories.SshPublicKeyFactory()
        self.client.force_authenticate(self.user)
        response = self.client.delete(factories.SshPublicKeyFactory.get_url(other_key))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_cannot_delete_shared_key(self):
        shared_key = factories.SshPublicKeyFactory(user=self.staff, is_shared=True)
        self.client.force_authenticate(self.user)

        response = self.client.delete(factories.SshPublicKeyFactory.get_url(shared_key))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_user_can_delete_shared_key(self):
        shared_key = factories.SshPublicKeyFactory(user=self.user, is_shared=True)
        self.client.force_authenticate(self.staff)
        response = self.client.delete(factories.SshPublicKeyFactory.get_url(shared_key))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_support_cannot_delete_other_users_key(self):
        # Support sees every key, but only reads them: a write reaches only
        # its own keys and the shared ones, like anyone else's.
        support = factories.UserFactory(is_support=True)
        self.client.force_authenticate(support)
        url = factories.SshPublicKeyFactory.get_url(self.user_key)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            core_models.SshPublicKey.objects.filter(pk=self.user_key.pk).exists()
        )

    def test_support_can_delete_own_key(self):
        support = factories.UserFactory(is_support=True)
        own_key = factories.SshPublicKeyFactory(user=support)
        self.client.force_authenticate(support)
        response = self.client.delete(factories.SshPublicKeyFactory.get_url(own_key))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


@override_config(PAT_ENABLED=True)
class SshKeyTokenTest(BaseSshKeyTest):
    """A personal access token acts as staff or support only with that scope."""

    def setUp(self):
        super().setUp()
        self.support = factories.UserFactory(is_support=True)
        self.shared_key = factories.SshPublicKeyFactory(user=self.user, is_shared=True)

    def use_token(self, user, *scopes):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(user, scopes=[scope.value for scope in scopes])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return factories.SshPublicKeyFactory(user=user)

    def listed(self):
        response = self.client.get(factories.SshPublicKeyFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def assert_other_users_key_kept_from(self, own_key):
        self.assertEqual(self.listed(), {own_key.uuid.hex, self.shared_key.uuid.hex})
        url = factories.SshPublicKeyFactory.get_url(self.user_key)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            core_models.SshPublicKey.objects.filter(pk=self.user_key.pk).exists()
        )

    def test_staff_token_without_staff_scope_sees_and_deletes_only_own_keys(self):
        own_key = self.use_token(self.staff, PermissionEnum.LIST_PROJECTS)
        self.assert_other_users_key_kept_from(own_key)
        response = self.client.delete(
            factories.SshPublicKeyFactory.get_url(self.shared_key)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.delete(factories.SshPublicKeyFactory.get_url(own_key))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_support_token_without_support_scope_sees_only_own_keys(self):
        own_key = self.use_token(self.support, PermissionEnum.LIST_PROJECTS)
        self.assert_other_users_key_kept_from(own_key)

    def test_staff_token_with_staff_scope_deletes_any_key(self):
        own_key = self.use_token(self.staff, PermissionEnum.STAFF_ACCESS)
        self.assertEqual(
            self.listed(),
            {own_key.uuid.hex, self.shared_key.uuid.hex, self.user_key.uuid.hex},
        )
        response = self.client.delete(
            factories.SshPublicKeyFactory.get_url(self.user_key)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_support_token_with_support_scope_sees_but_does_not_delete(self):
        own_key = self.use_token(self.support, PermissionEnum.SUPPORT_ACCESS)
        self.assertEqual(
            self.listed(),
            {own_key.uuid.hex, self.shared_key.uuid.hex, self.user_key.uuid.hex},
        )
        response = self.client.delete(
            factories.SshPublicKeyFactory.get_url(self.user_key)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            core_models.SshPublicKey.objects.filter(pk=self.user_key.pk).exists()
        )

    def test_support_token_with_support_scope_cannot_delete_shared_key(self):
        self.use_token(self.support, PermissionEnum.SUPPORT_ACCESS)
        response = self.client.delete(
            factories.SshPublicKeyFactory.get_url(self.shared_key)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            core_models.SshPublicKey.objects.filter(pk=self.shared_key.pk).exists()
        )


class SshKeyDeleteRuleTest(BaseSshKeyTest):
    """The delete rule is one check that both enforces and describes it.

    The view's queryset hides most keys a caller may not delete, so these
    call the check itself, as it runs if a key reaches it.
    """

    def check(self, user, key):
        request = SimpleNamespace(user=user, auth=None, method="DELETE")
        permissions.can_delete_ssh_key(request, view=None, obj=key)

    def test_destroy_route_states_the_rule(self):
        view = views.SshKeyViewSet(action="destroy", detail=True, kwargs={"uuid": "x"})
        route = access.access_for(view, "DELETE")
        self.assertTrue(route.described)
        self.assertEqual(
            route.entries,
            [
                access.any_of(
                    access.staff(),
                    access.owned("The caller's own SSH key, unless it is shared."),
                )
            ],
        )

    def test_support_may_not_delete_another_users_key(self):
        support = factories.UserFactory(is_support=True)
        with self.assertRaises(exceptions.PermissionDenied):
            self.check(support, self.user_key)

    def test_user_may_not_delete_another_users_key(self):
        with self.assertRaises(exceptions.PermissionDenied):
            self.check(factories.UserFactory(), self.user_key)

    def test_owner_may_not_delete_own_shared_key(self):
        self.user_key.is_shared = True
        with self.assertRaises(exceptions.PermissionDenied):
            self.check(self.user, self.user_key)

    def test_owner_and_staff_may_delete(self):
        self.check(self.user, self.user_key)
        self.check(self.staff, self.user_key)
        self.user_key.is_shared = True
        self.check(self.staff, self.user_key)


ED25519_PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHaD5EERMoDJvjH9p4wR19MFX6y"
    "+VI6J6432cI5x4PjT test@example.com"
)


class SshKeyTypeRestrictionTest(BaseSshKeyTest):
    @override_config(SSH_KEY_ALLOWED_TYPES=["ssh-ed25519"])
    def test_user_cannot_upload_disallowed_key_type(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {"name": key.name, "public_key": key.public_key}
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ssh-rsa", response.data["public_key"][0])

    @override_config(SSH_KEY_ALLOWED_TYPES=["ssh-rsa"])
    def test_user_can_upload_allowed_key_type(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {"name": key.name, "public_key": key.public_key}
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_config(SSH_KEY_ALLOWED_TYPES=[])
    def test_all_key_types_allowed_when_setting_empty(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {"name": key.name, "public_key": key.public_key}
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_config(SSH_KEY_MIN_RSA_KEY_SIZE=4096)
    def test_rsa_key_rejected_if_too_short(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {"name": key.name, "public_key": key.public_key}
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("2048", response.data["public_key"][0])
        self.assertIn("4096", response.data["public_key"][0])

    @override_config(SSH_KEY_MIN_RSA_KEY_SIZE=2048)
    def test_rsa_key_accepted_if_long_enough(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {"name": key.name, "public_key": key.public_key}
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_config(SSH_KEY_MIN_RSA_KEY_SIZE=0)
    def test_rsa_key_size_check_disabled_when_zero(self):
        self.client.force_authenticate(self.user)
        key = factories.SshPublicKeyFactory.build()
        data = {"name": key.name, "public_key": key.public_key}
        response = self.client.post(
            factories.SshPublicKeyFactory.get_list_url(), data=data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
