import unittest
from unittest import mock

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
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
            scope=self.fixture.tenant, type=OPENSTACK_INSTANCE_OFFERING
        )
        self.instance = self.fixture.instance

    def test_create_resources_for_lost_instances_and_volumes(self):
        tasks.create_resources_for_lost_instances_and_volumes()
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=self.instance).exists()
        )

    @mock.patch(
        "waldur_mastermind.marketplace_openstack.tasks.utils.import_instance_metadata"
    )
    def test_refresh_instance_backend_metadata_skips_instances_without_resource(
        self, mock_import_instance_metadata
    ):
        # self.instance exists but no marketplace Resource has been created for it.
        self.assertFalse(
            marketplace_models.Resource.objects.filter(scope=self.instance).exists()
        )

        # Task must not raise and must not call import_instance_metadata.
        tasks.refresh_instance_backend_metadata()

        mock_import_instance_metadata.assert_not_called()

    @mock.patch(
        "waldur_mastermind.marketplace_openstack.tasks.utils.import_instance_metadata"
    )
    def test_refresh_instance_backend_metadata_calls_import_for_linked_resource(
        self, mock_import_instance_metadata
    ):
        resource = marketplace_factories.ResourceFactory(
            offering=self.offering, scope=self.instance
        )
        # The post_save signal handler import_resource_metadata_when_resource_is_created
        # also invokes import_instance_metadata when the Resource is created. We're
        # exercising the periodic task here, so reset the mock to ignore that call.
        mock_import_instance_metadata.reset_mock()

        tasks.refresh_instance_backend_metadata()

        mock_import_instance_metadata.assert_called_once_with(resource)


@unittest.skip("Mock does not work correctly for backend")
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
            scope=self.instance.tenant, type=OPENSTACK_INSTANCE_OFFERING
        )
        self.volume_offering = marketplace_factories.OfferingFactory(
            scope=self.volume.tenant, type=OPENSTACK_VOLUME_OFFERING
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
        self.assertEqual(resource.state, ResourceStates.TERMINATED)
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
        self.assertEqual(resource.state, ResourceStates.TERMINATED)
        self.assertRaises(
            openstack_models.Volume.DoesNotExist, self.volume.refresh_from_db
        )
