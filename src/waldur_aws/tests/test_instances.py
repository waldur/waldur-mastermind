from unittest import mock

from rest_framework import status, test

from waldur_aws import models

from . import factories, fixtures


class InstanceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.AWSFixture()
        self.url = factories.InstanceFactory.get_list_url()

    @mock.patch('waldur_aws.executors.InstanceCreateExecutor.execute')
    def test_instance_is_created(self, executor_mock):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_payload()

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        executor_mock.assert_called_once()

    @mock.patch('waldur_aws.executors.InstanceCreateExecutor.execute')
    def test_spl_quotas_are_increased_when_instance_is_created(self, executor_mock):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_payload()

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        executor_mock.assert_called_once()
        instance = models.Instance.objects.get(name=payload['name'])
        spl = instance.service_project_link
        actual_storage_quota = spl.quotas.get(
            name=models.AWSServiceProjectLink.Quotas.storage
        ).usage
        actual_ram_quota = spl.quotas.get(
            name=models.AWSServiceProjectLink.Quotas.ram
        ).usage
        actual_vcpu_quota = spl.quotas.get(
            name=models.AWSServiceProjectLink.Quotas.vcpu
        ).usage
        self.assertEqual(self.fixture.size.disk, actual_storage_quota)
        self.assertEqual(self.fixture.size.ram, actual_ram_quota)
        self.assertEqual(self.fixture.size.cores, actual_vcpu_quota)

    def _get_valid_payload(self):
        return {
            'size': factories.SizeFactory.get_url(self.fixture.size),
            'image': factories.ImageFactory.get_url(self.fixture.image),
            'region': factories.RegionFactory.get_url(self.fixture.region),
            'service_project_link': factories.AWSServiceProjectLinkFactory.get_url(
                self.fixture.spl
            ),
            'name': 'aws-instance-name',
        }


class InstanceResizeTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.AWSFixture()

    @mock.patch('waldur_aws.executors.InstanceResizeExecutor.execute')
    def test_resize_increases_quotas_usage(self, executor):
        self.client.force_authenticate(self.fixture.owner)
        instance = self.fixture.instance
        instance.increase_backend_quotas_usage()
        size = factories.SizeFactory(
            cores=instance.cores + 2, ram=instance.ram + 1024, disk=instance.disk + 2048
        )
        size.regions.add(instance.region)

        payload = {
            'size': factories.SizeFactory.get_url(size),
        }

        response = self.client.post(
            factories.InstanceFactory.get_url(instance, 'resize'), payload
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        spl = instance.service_project_link
        actual_storage_quota = spl.quotas.get(
            name=models.AWSServiceProjectLink.Quotas.storage
        ).usage
        actual_ram_quota = spl.quotas.get(
            name=models.AWSServiceProjectLink.Quotas.ram
        ).usage
        actual_vcpu_quota = spl.quotas.get(
            name=models.AWSServiceProjectLink.Quotas.vcpu
        ).usage
        self.assertEqual(size.disk, actual_storage_quota)
        self.assertEqual(size.ram, actual_ram_quota)
        self.assertEqual(size.cores, actual_vcpu_quota)
