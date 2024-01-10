import collections
import unittest
from unittest import mock

from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.backend import ServiceBackend
from waldur_core.structure.tests import factories as structure_factories
from waldur_digitalocean import models

from . import factories


@unittest.skip("Move import to marketplace")
@mock.patch("digitalocean.Manager")
class ImportDropletTest(test.APITransactionTestCase):
    def setUp(self):
        self.import_url = factories.DropletFactory.get_url(self.link.service)
        self.project_url = structure_factories.ProjectFactory.get_url(self.link.project)
        self.client.force_authenticate(
            user=structure_factories.UserFactory(is_staff=True)
        )

        Droplet = collections.namedtuple(
            "Droplet",
            (
                "name",
                "vcpus",
                "memory",
                "disk",
                "ip_address",
                "status",
                "created_at",
                "size",
                "size_slug",
                "image",
            ),
        )
        self.mocked_droplet = Droplet(
            name="Webserver",
            vcpus=1,
            memory=1,
            disk=1,
            ip_address="10.0.0.1",
            status="active",
            size={"transfer": 1},
            size_slug="832959fe-4a87-4d0c-bf6b-b09d468daeb6",
            created_at=timezone.now().isoformat(),
            image={"distribution": "CentOS", "name": "7.1 x64"},
        )

    def test_user_can_import_droplet(self, mocked_manager):
        mocked_manager().get_droplet.return_value = self.mocked_droplet

        response = self.client.post(
            self.import_url, {"backend_id": "VALID_ID", "project": self.project_url}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_manager().get_droplet.assert_called_once_with("VALID_ID")

        droplet = models.Droplet.objects.get(uuid=response.data["uuid"])
        self.assertEqual(droplet.service_settings, self.service_settings)
        self.assertEqual(droplet.backend_id, "VALID_ID")
        self.assertEqual(droplet.state, models.Droplet.States.OK)
        self.assertEqual(droplet.runtime_state, models.Droplet.RuntimeStates.ONLINE)
        self.assertEqual(droplet.name, self.mocked_droplet.name)
        self.assertEqual(droplet.image_name, "CentOS 7.1 x64")
        self.assertEqual(droplet.cores, self.mocked_droplet.vcpus)
        self.assertEqual(droplet.ram, self.mocked_droplet.memory)
        self.assertEqual(droplet.disk, ServiceBackend.gb2mb(self.mocked_droplet.disk))
