from constance.test import override_config
from rest_framework import status, test

from waldur_core.core import models as core_models
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
        self.assertDictContainsSubset(
            {"name": ["This field must be unique."]}, response.data
        )

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
