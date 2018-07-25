from __future__ import unicode_literals

from django.test import TransactionTestCase
import mock

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_freeipa import models as freeipa_models

from .. import tasks
from . import fixtures


class SlurmAssociationSynchronizationTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.user = structure_factories.UserFactory()

        service_settings = self.fixture.service.settings
        service_settings.options = {'default_account': 'waldur_user'}
        service_settings.save()

        self.freeipa_profile = freeipa_models.Profile.objects.create(user=self.user, username='valid_username')
        self.serialized_profile = core_utils.serialize_instance(self.freeipa_profile)

        self.customer = self.fixture.customer
        self.serialized_customer = core_utils.serialize_instance(self.customer)

        self.project = self.fixture.project
        self.serialized_project = core_utils.serialize_instance(self.project)

    def test_when_customer_owner_role_is_granted_profile_is_synchronized(self):
        with mock.patch('waldur_slurm.tasks.process_role_granted') as mock_task:
            self.customer.add_user(self.user, structure_models.CustomerRole.OWNER)
            mock_task.delay.assert_called_once_with(self.serialized_profile, self.serialized_customer)

    def test_when_customer_owner_role_is_revoked_profile_is_synchronized(self):
        self.customer.add_user(self.user, structure_models.CustomerRole.OWNER)
        with mock.patch('waldur_slurm.tasks.process_role_revoked') as mock_task:
            self.customer.remove_user(self.user)
            mock_task.delay.assert_called_once_with(self.serialized_profile, self.serialized_customer)

    def test_customer_association_is_created_if_it_does_not_exist_yet(self):
        allocation = self.fixture.allocation
        self.customer.add_user(self.user, structure_models.CustomerRole.OWNER)

        with mock.patch('waldur_slurm.backend.SlurmClient') as mock_client:
            mock_client().get_association.return_value = False
            tasks.add_user(self.serialized_profile)
            account = 'waldur_allocation_%s' % allocation.uuid.hex
            mock_client().create_association.assert_called_once_with(
                self.freeipa_profile.username, account, 'waldur_user')

    def test_when_project_manager_role_is_granted_profile_is_synchronized(self):
        with mock.patch('waldur_slurm.tasks.process_role_granted') as mock_task:
            self.project.add_user(self.user, structure_models.ProjectRole.MANAGER)
            mock_task.delay.assert_called_once_with(self.serialized_profile, self.serialized_project)

    def test_when_project_manager_role_is_revoked_profile_is_synchronized(self):
        self.project.add_user(self.user, structure_models.ProjectRole.MANAGER)
        with mock.patch('waldur_slurm.tasks.process_role_revoked') as mock_task:
            self.project.remove_user(self.user)
            mock_task.delay.assert_called_once_with(self.serialized_profile, self.serialized_project)

    def test_project_association_is_created_if_it_does_not_exist_yet(self):
        allocation = self.fixture.allocation
        self.project.add_user(self.user, structure_models.ProjectRole.MANAGER)

        with mock.patch('waldur_slurm.backend.SlurmClient') as mock_client:
            mock_client().get_association.return_value = False
            tasks.add_user(self.serialized_profile)
            account = 'waldur_allocation_%s' % allocation.uuid.hex
            mock_client().create_association.assert_called_once_with(
                self.freeipa_profile.username, account, 'waldur_user')
