import responses
from django.test import override_settings
from rest_framework import test

from waldur_auth_social import tasks
from waldur_core.core import models as core_models
from waldur_core.structure.tests import factories


class SSHKeySyncTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.user = factories.UserFactory()
        self.key1 = factories.SshPublicKeyFactory(user=self.user)
        self.key2 = factories.SshPublicKeyFactory(user=self.user)
        self.new_ssh_key = factories.SshPublicKeyFactory()
        responses.add(
            responses.GET,
            "https://test.myaccessid.example.com/api/vo/puhuri/ssh_keys",
            json={
                "data": {
                    self.user.username: {
                        "ssh_keys": [self.key1.public_key, self.new_ssh_key.public_key]
                    }
                }
            },
        )

    @responses.activate
    @override_settings(
        WALDUR_AUTH_SOCIAL={
            'REMOTE_EDUTEAMS_SSH_API_URL': "https://test.myaccessid.example.com",
            'REMOTE_EDUTEAMS_SSH_API_USERNAME': "ssh_keys_testuser",
            'REMOTE_EDUTEAMS_SSH_API_PASSWORD': "secret_passw0rd!",
            'REMOTE_EDUTEAMS_ENABLED': True,
        }
    )
    def test_user_ssk_keys_sync(self):
        tasks.pull_remote_eduteams_ssh_keys()
        self.user.refresh_from_db()

        self.assertIsNone(
            core_models.SshPublicKey.objects.filter(
                user=self.user, name=self.key2.name
            ).first()
        )
        self.assertTrue(
            core_models.SshPublicKey.objects.filter(
                user=self.user, name=self.key1.name
            ).exists()
        )
        self.assertTrue(
            core_models.SshPublicKey.objects.filter(
                user=self.user, public_key=self.new_ssh_key.public_key
            ).exists()
        )
