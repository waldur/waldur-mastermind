from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests.factories import ProjectFactory, ServiceSettingsFactory
from waldur_digitalocean import models
from waldur_digitalocean.tests import factories, fixtures


class DropletResizeTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.DigitalOceanFixture()

    def test_user_can_not_resize_provisioning_droplet(self):
        self.client.force_authenticate(user=self.fixture.owner)

        droplet = factories.DropletFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cores=2,
            ram=2 * 1024,
            disk=10 * 1024,
            state=models.Droplet.States.UPDATING,
        )
        new_size = factories.SizeFactory(cores=3, ram=3 * 1024, disk=20 * 1024)

        response = self.client.post(
            factories.DropletFactory.get_url(droplet, "resize"),
            {"size": factories.SizeFactory.get_url(new_size), "disk": True},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_can_resize_droplet_to_bigger_size(self):
        self.client.force_authenticate(user=self.fixture.owner)

        droplet = factories.DropletFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cores=2,
            ram=2 * 1024,
            disk=10 * 1024,
            state=models.Droplet.States.OK,
            runtime_state=models.Droplet.RuntimeStates.OFFLINE,
        )
        new_size = factories.SizeFactory(cores=3, ram=3 * 1024, disk=20 * 1024)

        response = self.client.post(
            factories.DropletFactory.get_url(droplet, "resize"),
            {"size": factories.SizeFactory.get_url(new_size), "disk": True},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_user_can_resize_droplet_to_smaller_cpu(self):
        self.client.force_authenticate(user=self.fixture.owner)

        droplet = factories.DropletFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            ram=1024,
            cores=3,
            disk=20 * 1024,
            state=models.Droplet.States.OK,
            runtime_state=models.Droplet.RuntimeStates.OFFLINE,
        )
        new_size = factories.SizeFactory(ram=1024, cores=2, disk=20 * 1024)

        response = self.client.post(
            factories.DropletFactory.get_url(droplet, "resize"),
            {"size": factories.SizeFactory.get_url(new_size), "disk": False},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_user_can_not_resize_droplet_to_smaller_disk(self):
        self.client.force_authenticate(user=self.fixture.owner)

        droplet = factories.DropletFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cores=2,
            ram=1024,
            disk=20 * 1024,
            state=models.Droplet.States.OK,
            runtime_state=models.Droplet.RuntimeStates.OFFLINE,
        )
        new_size = factories.SizeFactory(
            cores=droplet.cores, ram=droplet.ram, disk=10 * 1024
        )

        response = self.client.post(
            factories.DropletFactory.get_url(droplet, "resize"),
            {"size": factories.SizeFactory.get_url(new_size), "disk": True},
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    @mock.patch("waldur_digitalocean.executors.DropletResizeExecutor.execute")
    def test_droplet_resize(self, executor):
        self.client.force_authenticate(self.fixture.owner)
        droplet = self.fixture.droplet
        droplet.runtime_state = droplet.RuntimeStates.OFFLINE
        droplet.save()
        droplet.increase_backend_quotas_usage()
        size = factories.SizeFactory(
            cores=droplet.cores + 2, disk=droplet.disk + 2048, ram=droplet.ram + 1024
        )
        payload = {
            "size": factories.SizeFactory.get_url(size),
            "disk": True,
        }

        response = self.client.post(
            factories.DropletFactory.get_url(droplet, "resize"), payload
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)


class DropletCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.DigitalOceanFixture()
        self.url = factories.DropletFactory.get_list_url()

    def test_droplet_is_created(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_payload()

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def _get_valid_payload(self):
        return {
            "name": "droplet-name",
            "service_settings": ServiceSettingsFactory.get_url(self.fixture.settings),
            "project": ProjectFactory.get_url(self.fixture.project),
            "image": factories.ImageFactory.get_url(self.fixture.image),
            "size": factories.SizeFactory.get_url(self.fixture.size),
            "region": factories.RegionFactory.get_url(self.fixture.region),
        }


class DropletDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.DigitalOceanFixture()

    @mock.patch("waldur_digitalocean.executors.DropletDeleteExecutor.execute")
    def test_droplet_deletion(self, delete_executor_mock):
        self.client.force_authenticate(self.fixture.owner)
        droplet = self.fixture.droplet
        response = self.client.delete(factories.DropletFactory.get_url(droplet))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        delete_executor_mock.assert_called_once()
