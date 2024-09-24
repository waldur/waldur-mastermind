from unittest import mock

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE, VOLUME_TYPE
from waldur_openstack import models as openstack_models
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests.fixtures import OpenStackFixture

from .. import tasks
from .utils import BaseOpenStackTest


class TaskTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.tenant, type=INSTANCE_TYPE
        )
        self.instance = self.fixture.instance

    def test_create_resources_for_lost_instances_and_volumes(self):
        tasks.create_resources_for_lost_instances_and_volumes()
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=self.instance).exists()
        )


@mock.patch("waldur_mastermind.marketplace_openstack.utils.OpenStackBackend")
class TaskSyncTenantTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.instance = openstack_factories.InstanceFactory()
        self.volume = openstack_factories.VolumeFactory()
        self.tenant = openstack_factories.TenantFactory(
            service_settings=self.instance.service_settings,
            project=self.instance.project,
        )
        self.instance_offering = marketplace_factories.OfferingFactory(
            scope=self.instance.tenant, type=INSTANCE_TYPE
        )
        self.volume_offering = marketplace_factories.OfferingFactory(
            scope=self.volume.tenant, type=VOLUME_TYPE
        )

    def test_sync_instances_if_tenant_has_been_synchronized(self, mock_backend):
        mock_backend().get_importable_instances.return_value = [
            {"backend_id": self.instance.backend_id}
        ]
        mock_backend().get_importable_volumes.return_value = []
        mock_backend().import_instance.return_value = self.instance
        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=self.instance).exists()
        )
        resource = marketplace_models.Resource.objects.get(scope=self.instance)

        # deleting of expired instance
        mock_backend().get_importable_instances.return_value = []
        mock_backend().get_expired_instances.return_value = [self.instance]
        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )
        resource.refresh_from_db()
        self.assertEqual(resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(
            openstack_models.Instance.DoesNotExist, self.instance.refresh_from_db
        )

    def test_sync_volumes_if_tenant_has_been_synchronized(self, mock_backend):
        mock_backend().get_importable_instances.return_value = []
        mock_backend().get_importable_volumes.return_value = [
            {"backend_id": self.volume.backend_id}
        ]
        mock_backend().import_volume.return_value = self.volume
        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=self.volume).exists()
        )
        resource = marketplace_models.Resource.objects.get(scope=self.volume)

        # deleting of expired instance
        mock_backend().get_importable_volumes.return_value = []
        mock_backend().get_expired_instances.return_value = [self.volume]
        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )
        resource.refresh_from_db()
        self.assertEqual(resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(
            openstack_models.Volume.DoesNotExist, self.volume.refresh_from_db
        )
