from __future__ import unicode_literals

from rest_framework import test, status

from . import factories, fixtures
from .. import models


def pluck(fields, row):
    return {field: row[field] for field in fields}


def clean_row(row):
    return pluck(('name', 'running_instances_count', 'created_instances_count'), row)


def clean_rows(rows):
    return sorted(map(clean_row, rows), key=lambda k: k['name'])


class TestImageUsageStats(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.admin = self.fixture.staff
        factories.InstanceFactory(
            volumes__image_name='Ubuntu 16.04',
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            service_project_link=self.fixture.spl)
        factories.InstanceFactory(
            volumes__image_name='Ubuntu 16.04',
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            service_project_link=self.fixture.spl)
        factories.InstanceFactory(
            volumes__image_name='Windows 10',
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            service_project_link=self.fixture.spl)
        factories.ImageFactory(
            name='Ubuntu 16.04',
            settings=self.fixture.openstack_tenant_service_settings)
        factories.ImageFactory(
            name='Centos 10.04',
            settings=self.fixture.openstack_tenant_service_settings)
        factories.ImageFactory(
            name='Windows 10',
            settings=self.fixture.openstack_tenant_service_settings)

    def test_usage_stats(self):
        expected = [
            {
                'name': 'Centos 10.04',
                'running_instances_count': 0,
                'created_instances_count': 0
            },
            {
                'name': 'Windows 10',
                'running_instances_count': 1,
                'created_instances_count': 0
            },
            {
                'name': 'Ubuntu 16.04',
                'running_instances_count': 1,
                'created_instances_count': 1
            }
        ]
        self.client.force_authenticate(user=self.admin)

        url = factories.ImageFactory.get_list_url(action='usage_stats')
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertListEqual(clean_rows(expected), clean_rows(response.data))


class TestFlavorUsageStats(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.admin = self.fixture.staff
        factories.InstanceFactory(
            flavor_name='Small',
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            service_project_link=self.fixture.spl)
        factories.InstanceFactory(
            flavor_name='Small',
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            service_project_link=self.fixture.spl)
        factories.InstanceFactory(
            flavor_name='Large',
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            service_project_link=self.fixture.spl)

        factories.FlavorFactory(name='Small',
                                settings=self.fixture.openstack_tenant_service_settings)
        factories.FlavorFactory(name='Medium',
                                settings=self.fixture.openstack_tenant_service_settings)
        factories.FlavorFactory(name='Large',
                                settings=self.fixture.openstack_tenant_service_settings)

    def test_usage_stats(self):
        expected = [
            {
                'running_instances_count': 1,
                'created_instances_count': 0,
                'name': 'Large'
            },
            {
                'running_instances_count': 0,
                'created_instances_count': 0,
                'name': 'Medium'
            },
            {
                'running_instances_count': 1,
                'created_instances_count': 1,
                'name': 'Small'
            }
        ]
        self.client.force_authenticate(user=self.admin)

        url = factories.FlavorFactory.get_list_url(action='usage_stats')
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertListEqual(clean_rows(expected), clean_rows(response.data))
