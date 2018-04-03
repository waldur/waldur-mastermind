from __future__ import unicode_literals

import json

import mock
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.test import APITransactionTestCase
from waldur_ansible.playbook_jobs.tests import factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.ansible_estimator.tests.fixtures import EstimationFixture
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack_tenant.tests import factories as tenant_factories

Types = package_models.PackageComponent.Types


class EstimatorTest(APITransactionTestCase):

    def setUp(self):
        self.fixture = EstimationFixture()
        self.template = self.fixture.template

        self.private_settings = self.fixture.private_settings
        self.private_service = self.fixture.private_service

        self.private_link = self.fixture.private_link
        self.private_link_url = tenant_factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.private_link)

        self.image = self.fixture.image
        self.image_url = tenant_factories.ImageFactory.get_url(self.image)

        self.flavor = self.fixture.flavor
        self.flavor_url = tenant_factories.FlavorFactory.get_url(self.flavor)

        self.subnet = self.fixture.subnet
        self.subnet_url = tenant_factories.SubNetFactory.get_url(self.subnet)

        self.prices = self.fixture.prices

        self.playbook = factories.PlaybookFactory()
        self.playbook_url = factories.PlaybookFactory.get_url(self.playbook)

        self.ssh_public_key = structure_factories.SshPublicKeyFactory(user=self.fixture.owner)
        self.ssh_public_key_url = structure_factories.SshPublicKeyFactory.get_url(self.ssh_public_key)

        self.internal_key = structure_factories.SshPublicKeyFactory(user=self.fixture.staff)
        self.internal_key_url = structure_factories.SshPublicKeyFactory.get_url(self.internal_key)

        self.path_patcher = mock.patch('os.path.exists')
        self.path_api = self.path_patcher.start()
        self.path_api.side_effect = lambda f: f == self.playbook.get_playbook_path()

        self.subprocess_patcher = mock.patch('subprocess.check_output')
        self.subprocess_api = self.subprocess_patcher.start()

        self.subprocess_api.return_value = self.get_valid_output()

    def tearDown(self):
        super(EstimatorTest, self).tearDown()
        self.path_patcher.stop()
        self.subprocess_patcher.stop()

    def test_user_can_get_estimation_report_for_valid_request(self):
        response = self.get_report()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data, self.get_expected_report())

    def test_validation_error_if_image_is_not_enough(self):
        self.image.min_ram = self.flavor.ram + 1
        self.image.save()

        response = self.get_report()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('flavor' in response.data[0])

    def test_validation_error_if_quota_exceeded(self):
        self.private_settings.quotas.filter(
            name=self.private_settings.Quotas.instances
        ).update(
            limit=10,
            usage=10
        )

        response = self.get_report()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('One or more quotas were exceeded' in response.data[0])

    def test_if_package_is_not_defined_price_is_zero(self):
        self.private_settings.scope = None
        self.private_settings.save()

        response = self.get_report()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['cost'], 0)

    def get_report(self):
        self.client.force_login(self.fixture.owner)
        url = reverse('ansible-estimator')
        return self.client.post(url, {
            'playbook': self.playbook_url,
            'service_project_link': self.private_link_url,
            'ssh_public_key': self.ssh_public_key_url,
        })

    def get_valid_output(self):
        return 'ok: [localhost] => %s' % json.dumps({
            'WALDUR_CHECK_MODE': True,
            'service_project_link': self.private_link_url,
            'ssh_public_key': self.internal_key_url,
            'flavor': self.flavor_url,
            'image': self.image_url,
            'name': 'Valid name',
            'system_volume_size': self.image.min_disk,
            'internal_ips_set': [
                {'subnet': self.subnet_url}
            ]
        })

    def get_expected_requirements(self):
        return {
            'cpu': self.flavor.cores,
            'ram': self.flavor.ram,
            'disk': self.flavor.disk,
        }

    def get_expected_prices(self):
        return {
            'cpu': self.prices[Types.CORES],
            'ram': self.prices[Types.RAM],
            'disk': self.prices[Types.STORAGE],
        }

    def get_expected_cost(self):
        return (
            self.flavor.cores * self.prices[Types.CORES] +
            self.flavor.ram * self.prices[Types.RAM] +
            self.flavor.disk * self.prices[Types.STORAGE]
        )

    def get_expected_report(self):
        return {
            'requirements': self.get_expected_requirements(),
            'prices': self.get_expected_prices(),
            'cost': self.get_expected_cost()
        }
