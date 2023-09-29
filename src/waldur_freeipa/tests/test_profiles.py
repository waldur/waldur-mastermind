from unittest import mock

from ddt import data, ddt
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_freeipa import tasks
from waldur_freeipa.backend import FreeIPABackend
from waldur_freeipa.tests import factories
from waldur_freeipa.tests.helpers import override_plugin_settings
from waldur_slurm import models as slurm_models
from waldur_slurm import signals as slurm_signals
from waldur_slurm.tests import fixtures as slurm_fixtures


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
        response = self.client.post(self.url, {'username': 'root'})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('username', response.data)

    def test_profile_should_be_unique(self):
        factories.ProfileFactory(user=self.user)
        response = self.client.post(self.url, {'username': 'VALID'})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('details', response.data)


@mock.patch('python_freeipa.Client')
class ProfileCreateTest(BaseProfileTest):
    def setUp(self):
        super().setUp()
        self.valid_data = {'username': 'alice'}

    def test_profile_creation_fails_if_username_is_not_available(self, mock_client):
        mock_client().user_add.side_effect = freeipa_exceptions.DuplicateEntry()
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn('username', response.data)

    def test_if_profile_created_client_is_called(self, mock_client):
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        mock_client().user_add.assert_called_once()

    def test_profile_is_not_active_initially(self, mock_client):
        response = self.client.post(self.url, self.valid_data)
        self.assertFalse(response.data['is_active'])
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
            disabled=True,
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
        ('Alex Bloggs', 'Alex', 'Bloggs'),
        ('Alex', 'Alex', ''),
        ('', '', ''),
    )
    def test_backend_is_called_with_correct_parameters_if_update_full_name(
        self, names, mock_client
    ):
        full_name = names[0]
        first_name = names[1]
        last_name = names[2]

        user = self.profile.user
        user.full_name = full_name
        user.first_name = first_name
        user.last_name = last_name
        user.save()
        self.profile.refresh_from_db()

        FreeIPABackend().update_user(self.profile)
        mock_client().user_mod.assert_called_once_with(
            self.profile.username,
            cn=full_name,
            displayname=full_name,
            givenname=first_name or 'N/A',
            sn=last_name or 'N/A',
            mail=user.email,
            organization_unit=user.organization,
            job_title=user.job_title,
            preferred_language=user.preferred_language,
            telephonenumber=user.phone_number,
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


@override_plugin_settings(ENABLED=True)
@mock.patch('waldur_freeipa.handlers.tasks')
class UpdateUserHandlerTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.profile = factories.ProfileFactory(user=self.user, is_active=True)

    def test_update_user_name(self, mock_task):
        user = self.profile.user
        user.first_name = 'Alex'
        user.last_name = 'Bloggs'
        user.save()

        mock_task.update_user.delay.assert_called_once_with(
            core_utils.serialize_instance(self.profile)
        )

    def test_update_user_email(self, mock_task):
        user = self.profile.user
        user.email = 'alex@gmail.com'
        user.save()

        mock_task.update_user.delay.assert_called_once_with(
            core_utils.serialize_instance(self.profile)
        )


@override_plugin_settings(ENABLED=True)
class ProfileAllocationTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.profile = factories.ProfileFactory(user=self.user, is_active=False)
        self.fixture = slurm_fixtures.SlurmFixture()

    def test_when_association_is_created_profile_is_enabled(self):
        self.fixture.allocation.project.add_user(self.user, ProjectRole.ADMIN)

        slurm_signals.slurm_association_created.send(
            slurm_models.Allocation,
            allocation=self.fixture.allocation,
            user=self.user,
            username=self.user.username,
        )
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_active)

    def test_active_profile_is_disabled_if_user_does_not_have_access_to_any_allocation(
        self,
    ):
        self.profile.is_active = True
        self.profile.save()

        tasks.disable_accounts_without_allocations()

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_active)

    def test_active_profile_is_not_disabled_if_user_has_access_to_any_allocation(self):
        self.profile.is_active = True
        self.profile.save()

        self.fixture.allocation.project.add_user(self.user, ProjectRole.ADMIN)

        tasks.disable_accounts_without_allocations()

        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_active)


@override_plugin_settings(ENABLED=True)
@mock.patch('python_freeipa.Client')
class ProfileStatusTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()

    def test_profile_is_enabled(self, mock_client):
        self.profile = factories.ProfileFactory(user=self.user, is_active=True)
        mock_client().user_find.return_value = {
            'result': [{'uid': [self.profile.username], 'nsaccountlock': True}]
        }

        FreeIPABackend().synchronize_groups()

        mock_client().user_enable.assert_called_once_with(self.profile.username)

    def test_profile_is_disabled(self, mock_client):
        self.profile = factories.ProfileFactory(user=self.user, is_active=False)
        mock_client().user_find.return_value = {
            'result': [{'uid': [self.profile.username], 'nsaccountlock': False}]
        }

        FreeIPABackend().synchronize_groups()

        mock_client().user_disable.assert_called_once_with(self.profile.username)

    def test_profile_is_not_disabled(self, mock_client):
        self.profile = factories.ProfileFactory(user=self.user, is_active=False)
        mock_client().user_find.return_value = {
            'result': [{'uid': [self.profile.username], 'nsaccountlock': True}]
        }

        FreeIPABackend().synchronize_groups()

        self.assertEqual(0, mock_client().user_disable.call_count)
        self.assertEqual(0, mock_client().user_enable.call_count)

    def test_profile_is_not_enabled(self, mock_client):
        self.profile = factories.ProfileFactory(user=self.user, is_active=True)
        mock_client().user_find.return_value = {
            'result': [{'uid': [self.profile.username], 'nsaccountlock': False}]
        }

        FreeIPABackend().synchronize_groups()

        self.assertEqual(0, mock_client().user_disable.call_count)
        self.assertEqual(0, mock_client().user_enable.call_count)

    def test_profile_is_skipped(self, mock_client):
        self.profile = factories.ProfileFactory(user=self.user, is_active=True)
        mock_client().user_find.return_value = {'result': []}

        FreeIPABackend().synchronize_groups()

        self.assertEqual(0, mock_client().user_disable.call_count)
        self.assertEqual(0, mock_client().user_enable.call_count)
