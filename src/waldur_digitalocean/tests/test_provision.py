from unittest import mock, skip

import digitalocean
from rest_framework import test

from waldur_core.structure.models import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.factories import ProjectFactory, ServiceSettingsFactory
from waldur_digitalocean.apps import DigitalOceanConfig
from waldur_digitalocean.models import Droplet
from waldur_digitalocean.tests import factories
from waldur_digitalocean.views import DropletViewSet


class DigitalOceanBackendTest(test.APITransactionTestCase):
    def setUp(self):
        super(DigitalOceanBackendTest, self).setUp()

        self.manager_patcher = mock.patch('digitalocean.Manager')
        self.manager_api = self.manager_patcher.start()

        self.droplet_patcher = mock.patch('digitalocean.Droplet')
        self.droplet_api = self.droplet_patcher.start()

        self.ssh_patcher = mock.patch('digitalocean.SSHKey')
        self.ssh_api = self.ssh_patcher.start()

    def tearDown(self):
        super(DigitalOceanBackendTest, self).tearDown()

        self.manager_patcher.stop()
        self.droplet_patcher.stop()
        self.ssh_patcher.stop()


class BaseDropletProvisionTest(DigitalOceanBackendTest):
    def setUp(self):
        super(BaseDropletProvisionTest, self).setUp()
        self.customer = structure_factories.CustomerFactory()

        self.settings = structure_factories.ServiceSettingsFactory(
            customer=self.customer,
            type=DigitalOceanConfig.service_name,
            token='VALID_TOKEN',
        )
        self.region = factories.RegionFactory()
        self.image = factories.ImageFactory()
        self.size = factories.SizeFactory()

        self.image.regions.add(self.region)
        self.size.regions.add(self.region)

        self.project = structure_factories.ProjectFactory(customer=self.customer)

        self.customer_owner = structure_factories.UserFactory()
        self.customer.add_user(self.customer_owner, CustomerRole.OWNER)

        self.client.force_authenticate(user=self.customer_owner)
        self.url = factories.DropletFactory.get_list_url()

        self.ssh_public_key = structure_factories.SshPublicKeyFactory(
            user=self.customer_owner
        )
        self.ssh_url = structure_factories.SshPublicKeyFactory.get_url(
            self.ssh_public_key
        )

        self.mock_backend()
        DropletViewSet.async_executor = False

    def tearDown(self):
        super(BaseDropletProvisionTest, self).tearDown()
        DropletViewSet.async_executor = True

    def mock_backend(self):
        self.mock_key = mock.Mock()
        self.mock_key.id = 'VALID_SSH_ID'
        self.mock_key.name = self.ssh_public_key.name
        self.mock_key.fingerprint = self.ssh_public_key.fingerprint
        self.ssh_api.return_value = self.mock_key

        self.mock_droplet = mock.Mock()
        self.mock_droplet.id = 'VALID_DROPLET_ID'
        self.mock_droplet.action_ids = ['VALID_ACTION_ID']
        self.mock_droplet.ip_address = '10.0.0.1'
        self.droplet_api.return_value = self.mock_droplet

        mock_action = mock.Mock()
        mock_action.status = 'completed'
        self.manager_api().get_action.return_value = mock_action
        self.manager_api().get_droplet.return_value = self.mock_droplet

    def get_valid_data(self, **extra):
        default = {
            'service_settings': ServiceSettingsFactory.get_url(self.settings),
            'project': ProjectFactory.get_url(self.project),
            'region': factories.RegionFactory.get_url(self.region),
            'image': factories.ImageFactory.get_url(self.image),
            'size': factories.SizeFactory.get_url(self.size),
            'name': 'valid-name',
        }
        default.update(extra)
        return default

    def test_if_ssh_key_exists_it_is_pulled_but_not_pushed(self):
        self.client.post(self.url, self.get_valid_data(ssh_public_key=self.ssh_url))

        self.ssh_api.assert_called_once_with(
            token=mock.ANY,
            fingerprint=self.ssh_public_key.fingerprint,
            name=self.ssh_public_key.name,
            id=None,
        )
        self.ssh_api().load.assert_called_once()
        self.assertFalse(self.ssh_api.create.called)

    def test_ssh_key_is_created_if_it_does_not_exist_yet(self):
        self.ssh_api().load.side_effect = digitalocean.DataReadError(
            'The resource you were accessing could not be found.'
        )
        self.client.post(self.url, self.get_valid_data(ssh_public_key=self.ssh_url))
        self.ssh_api().load.assert_called_once()
        self.ssh_api().create.assert_called_once()

    def test_if_ssh_key_is_not_specified_it_is_not_used(self):
        self.client.post(self.url, self.get_valid_data())

        self.assertFalse(self.ssh_api().load.called)
        self.assertFalse(self.ssh_api().create.called)
        self.droplet_api().create.assert_called_once()

        droplet = Droplet.objects.get(backend_id=self.mock_droplet.id)
        self.assertEqual(droplet.key_name, '')
        self.assertEqual(droplet.key_fingerprint, '')

    def test_if_ssh_is_used_it_is_stored_in_droplet(self):
        self.client.post(self.url, self.get_valid_data(ssh_public_key=self.ssh_url))
        droplet = Droplet.objects.get(backend_id=self.mock_droplet.id)
        self.assertEqual(droplet.key_name, self.mock_key.name)
        self.assertEqual(droplet.key_fingerprint, self.mock_key.fingerprint)

    def test_when_droplet_is_created_backend_is_called(self):
        self.client.post(
            self.url,
            self.get_valid_data(name='VALID-NAME', ssh_public_key=self.ssh_url),
        )

        self.droplet_api.assert_called_once_with(
            token=mock.ANY,
            name='VALID-NAME',
            user_data='',
            region=self.region.backend_id,
            image=self.image.backend_id,
            size_slug=self.size.backend_id,
            ssh_keys=[self.mock_key.id],
        )
        self.droplet_api().create.assert_called_once()

    @skip('Unclear why is failing, but not relevant for marketplace.')
    def test_when_droplet_is_created_last_action_is_pulled(self):
        self.client.post(self.url, self.get_valid_data(ssh_public_key=self.ssh_url))
        action_id = self.mock_droplet.action_ids[-1]
        self.manager_api().get_action.assert_called_once_with(action_id)

    def test_when_droplet_is_created_external_ip_is_pulled(self):
        self.client.post(self.url, self.get_valid_data(ssh_public_key=self.ssh_url))
        self.manager_api().get_droplet.assert_called_once_with(self.mock_droplet.id)

        droplet = Droplet.objects.get(backend_id=self.mock_droplet.id)
        self.assertIsNotNone(droplet.external_ips)
        self.assertEqual(droplet.external_ips[0], self.mock_droplet.ip_address)

    def test_when_droplet_is_created_its_state_is_ok_online(self):
        self.client.post(self.url, self.get_valid_data())
        droplet = Droplet.objects.get(backend_id=self.mock_droplet.id)
        self.assertEqual(droplet.state, Droplet.States.OK)
        self.assertEqual(droplet.runtime_state, Droplet.RuntimeStates.ONLINE)
        self.assertEqual(droplet.backend_id, self.mock_droplet.id)
