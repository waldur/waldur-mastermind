from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_mastermind.slurm_invoices import models as slurm_invoices_models


class SlurmPackageTest(test.APITransactionTestCase):
    def create_package(self):
        fixture = structure_fixtures.ProjectFixture()
        url = marketplace_factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(fixture.staff)
        category = marketplace_factories.CategoryFactory()

        payload = {
            'name': 'offering',
            'type': PLUGIN_NAME,
            'category': marketplace_factories.CategoryFactory.get_url(
                category=category
            ),
            'customer': structure_factories.CustomerFactory.get_url(
                customer=fixture.customer
            ),
            'service_attributes': {
                'hostname': 'example.com',
                'username': 'root',
                'port': 8888,
                'gateway': 'gw.example.com',
                'use_sudo': 'true',
                'default_account': 'TEST',
            },
            'plans': [
                {
                    'name': 'default',
                    'description': 'default plan',
                    'unit': UnitPriceMixin.Units.QUANTITY,
                    'unit_price': 100,
                    'prices': {'cpu': 10, 'gpu': 100, 'ram': 1000,},
                }
            ],
        }
        with mock.patch('waldur_core.structure.models.ServiceSettings.get_backend'):
            response = self.client.post(url, payload)
        return response

    def test_create_slurm_package(self):
        response = self.create_package()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        package = slurm_invoices_models.SlurmPackage.objects.last()
        self.assertEqual(package.cpu_price, 10)
        self.assertEqual(package.gpu_price, 100)
        self.assertEqual(package.ram_price, 1000)

    def test_component_price_is_synchronized(self):
        response = self.create_package()
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        component = offering.plans.first().components.get(component__type='cpu')
        component.price += 1
        component.save()
        package = slurm_invoices_models.SlurmPackage.objects.last()
        self.assertEqual(package.cpu_price, component.price)
