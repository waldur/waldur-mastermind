from unittest import mock

from ddt import data, ddt
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_freeipa import tasks
from waldur_freeipa.backend import FreeIPABackend
from waldur_freeipa.tests import factories
from waldur_freeipa.tests.helpers import override_plugin_settings


@override_plugin_settings(ENABLED=True)
class BaseProfileTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(preferred_language='ET')
        self.client.force_authenticate(self.user)
        self.url = factories.ProfileFactory.get_list_url()


class ProfileValidateTest(BaseProfileTest):
    def test_username_should_not_contain_spaces(self):
        response = self.client.post(self.url, {'username': 'Alice Lebowski'})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('username', response.data)

    def test_username_should_not_contain_special_characters(self):
        response = self.client.post(self.url, {'username': '#$%^?'})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('username', response.data)

    def test_username_should_not_exceed_limit(self):
        response = self.client.post(self.url, {'username': 'abc' * 300})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('username', response.data)

    @override_plugin_settings(ENABLED=True, BLACKLISTED_USERNAMES=['root'])
    def test_blacklisted_username_is_not_allowed(self):
        response = self.client.post(
            self.url, {'username': 'root', 'agree_with_policy': True}
        )
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('username', response.data)

    def test_user_should_agree_with_policy(self):
        response = self.client.post(
            self.url, {'username': 'VALID', 'agree_with_policy': False}
        )
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('agree_with_policy', response.data)

    def test_profile_should_be_unique(self):
        factories.ProfileFactory(user=self.user)
        response = self.client.post(
            self.url, {'username': 'VALID', 'agree_with_policy': True}
        )
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('details', response.data)


@mock.patch('python_freeipa.Client')
class ProfileCreateTest(BaseProfileTest):
    def setUp(self):
        super(ProfileCreateTest, self).setUp()
        self.valid_data = {'username': 'alice', 'agree_with_policy': True}

    def test_profile_creation_fails_if_username_is_not_available(self, mock_client):
        mock_client().user_add.side_effect = freeipa_exceptions.DuplicateEntry()
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('username', response.data)

    def test_if_profile_created_client_is_called(self, mock_client):
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        mock_client().user_add.assert_called_once()

    def test_profile_is_active_initially(self, mock_client):
        response = self.client.post(self.url, self.valid_data)
        self.assertTrue(response.data['is_active'])
        self.assertIsNotNone(response.data['agreement_date'])

    @override_plugin_settings(ENABLED=True, USERNAME_PREFIX='ipa_')
    def test_username_is_prefixed(self, mock_client):
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        self.assertEqual('ipa_alice', response.data['username'])

    def test_backend_is_called_with_correct_parameters(self, mock_client):
        self.client.post(self.url, self.valid_data)
        mock_client().user_add.assert_called_once_with(
            username='waldur_alice',
            first_name=self.user.full_name.split()[0],
            last_name=self.user.full_name.split()[-1],
            full_name=self.user.full_name,
            mail=self.user.email,
            job_title=self.user.job_title,
            telephonenumber=self.user.phone_number,
            preferred_language=self.user.preferred_language,
            ssh_key=[],
            gecos=','.join(
                [self.user.full_name, self.user.email, self.user.phone_number]
            ),
            user_password=None,
            organization_unit=self.user.organization,
        )

    def test_when_profile_created_ssh_keys_are_attached(self, mock_client):
        ssh_keys = structure_factories.SshPublicKeyFactory.create_batch(
            3, user=self.user
        )
        expected_keys = [key.public_key for key in ssh_keys]

        self.client.post(self.url, self.valid_data)

        args, kwargs = mock_client().user_add.call_args
        self.assertEqual(sorted(expected_keys), sorted(kwargs.get('ssh_key')))


@override_plugin_settings(ENABLED=True)
@mock.patch('python_freeipa.Client')
class ProfileDisableTest(test.APITransactionTestCase):
    def setUp(self):
        self.waldur_user = structure_factories.UserFactory()
        self.freeipa_user = factories.ProfileFactory(user=self.waldur_user)
        self.client.force_authenticate(self.waldur_user)
        self.url = factories.ProfileFactory.get_url(self.freeipa_user, 'disable')

    def test_user_can_disable_his_profile(self, mock_client):
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_disable.assert_called_once()

    def test_if_profile_already_disabled_on_backend_model_is_updated(self, mock_client):
        mock_client().user_disable.side_effect = freeipa_exceptions.AlreadyInactive()

        response = self.client.post(self.url)

        self.freeipa_user.refresh_from_db()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertFalse(self.freeipa_user.is_active)

    def test_staff_can_disable_profile_for_any_user(self, mock_client):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_disable.assert_called_once()

    def test_profile_can_not_disable_his_profile_twice(self, mock_client):
        self.freeipa_user.is_active = False
        self.freeipa_user.save()

        response = self.client.post(self.url)

        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        mock_client().user_disable.assert_not_called()

    def test_profile_can_not_disable_profile_for_other_user(self, mock_client):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)

        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)
        mock_client().user_disable.assert_not_called()


@override_plugin_settings(ENABLED=True)
@mock.patch('python_freeipa.Client')
class ProfileEnableTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.profile = factories.ProfileFactory(user=self.user, is_active=False)
        self.client.force_authenticate(self.user)
        self.url = factories.ProfileFactory.get_url(self.profile, 'enable')

    def test_user_can_enable_his_profile(self, mock_client):
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_enable.assert_called_once()

    def test_if_profile_already_enabled_on_backend_model_is_updated(self, mock_client):
        mock_client().user_disable.side_effect = freeipa_exceptions.AlreadyActive()

        response = self.client.post(self.url)

        self.profile.refresh_from_db()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertTrue(self.profile.is_active)

    def test_staff_can_enable_profile_for_any_user(self, mock_client):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_enable.assert_called_once()

    def test_profile_can_not_enable_his_profile_twice(self, mock_client):
        self.profile.is_active = True
        self.profile.save()

        response = self.client.post(self.url)

        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        mock_client().user_enable.assert_not_called()

    def test_profile_can_not_enable_profile_for_other_user(self, mock_client):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)

        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)
        mock_client().user_enable.assert_not_called()


@override_plugin_settings(ENABLED=True)
@mock.patch('python_freeipa.Client')
class ProfileSshKeysTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.profile = factories.ProfileFactory(user=self.user, is_active=False)

        self.ssh_keys = structure_factories.SshPublicKeyFactory.create_batch(
            3, user=self.user
        )
        self.expected_keys = [key.public_key for key in self.ssh_keys]

    def update_keys(self):
        self.client.force_authenticate(self.user)
        url = factories.ProfileFactory.get_url(self.profile, 'update_ssh_keys')
        return self.client.post(url)

    def test_user_can_update_ssh_keys_for_his_profile(self, mock_client):
        response = self.update_keys()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_mod.assert_called_once_with(
            self.profile.username, ipasshpubkey=self.expected_keys
        )

    def test_if_profile_has_same_ssh_keys_profile_is_not_updated(self, mock_client):
        mock_client().user_show.return_value = {'ipasshpubkey': self.expected_keys}
        response = self.update_keys()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_mod.assert_not_called()

    def test_if_keys_are_sorted_before_comparison(self, mock_client):
        mock_client().user_show.return_value = {
            'ipasshpubkey': sorted(self.expected_keys, reverse=True)
        }
        response = self.update_keys()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_mod.assert_not_called()

    def test_empty_keys_list_is_processed_correctly(self, mock_client):
        mock_client().user_show.return_value = {}
        self.user.sshpublickey_set.all().delete()

        response = self.update_keys()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        mock_client().user_mod.assert_not_called()

    def test_task_is_scheduled_when_key_is_created(self, mock_client):
        with mock.patch('waldur_freeipa.tasks.sync_profile_ssh_keys') as mock_task:
            structure_factories.SshPublicKeyFactory(user=self.user)
            mock_task.delay.assert_called_once_with(self.profile.id)

    def test_task_is_scheduled_when_key_is_deleted(self, mock_client):
        with mock.patch('waldur_freeipa.tasks.sync_profile_ssh_keys') as mock_task:
            self.user.sshpublickey_set.first().delete()
            mock_task.delay.assert_called_once_with(self.profile.id)

    def test_profile_is_updated_when_task_is_called(self, mock_client):
        mock_client().user_show.return_value = {'ipasshpubkey': self.expected_keys}
        self.user.sshpublickey_set.all().delete()

        tasks.sync_profile_ssh_keys(self.profile.id)
        mock_client().user_mod.assert_called_once_with(
            self.profile.username, ipasshpubkey=None
        )


@ddt
@override_plugin_settings(ENABLED=True)
@mock.patch('python_freeipa.Client')
class ProfileUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.profile = factories.ProfileFactory(user=self.user, is_active=False)

    @data(
        ('Alex Bloggs', 'Alex', 'Bloggs', 'AB'),
        ('Alex', 'Alex', 'N/A', 'A'),
        ('', 'N/A', 'N/A', ''),
    )
    def test_backend_is_called_with_correct_parameters_if_update_full_name(
        self, names, mock_client
    ):
        full_name = names[0]
        first_name = names[1]
        last_name = names[2]
        initials = names[3]

        user = self.profile.user
        user.full_name = full_name
        user.save()
        self.profile.refresh_from_db()

        FreeIPABackend().update_name(self.profile)
        mock_client().user_mod.assert_called_once_with(
            self.profile.username,
            cn=full_name,
            displayname=full_name,
            givenname=first_name,
            initials=initials,
            sn=last_name,
        )

    def test_backend_is_called_with_correct_parameters_if_update_gecos(
        self, mock_client
    ):
        FreeIPABackend().update_gecos(self.profile)
        mock_client().user_mod.assert_called_once_with(
            self.profile.username,
            gecos=','.join(
                [self.user.full_name, self.user.email, self.user.phone_number]
            ),
        )
