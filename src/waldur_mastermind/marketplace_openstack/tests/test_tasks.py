import mock

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests.fixtures import OpenStackTenantFixture

from .. import tasks
from .utils import BaseOpenStackTest


class TaskTest(BaseOpenStackTest):
    def setUp(self):
        super(TaskTest, self).setUp()
        self.fixture = OpenStackTenantFixture()
        self.offering = marketplace_factories.OfferingFactory()
        self.offering.scope = self.fixture.instance.service_settings
        self.offering.type = INSTANCE_TYPE
        self.offering.save()

    def test_create_resources_for_lost_instances_and_volumes(self):
        tasks.create_resources_for_lost_instances_and_volumes()
        self.assertTrue(
            marketplace_models.Resource.objects.filter(offering=self.offering).exists()
        )


class TaskSyncTenantTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.instance = openstack_tenant_factories.InstanceFactory()
        self.tenant = openstack_factories.TenantFactory(
            service_settings=self.instance.service_settings,
            project=self.instance.project,
        )
        self.offering = marketplace_factories.OfferingFactory()
        self.offering.scope = self.instance.service_settings
        self.offering.type = INSTANCE_TYPE
        self.offering.save()

    @mock.patch(
        'waldur_mastermind.marketplace_openstack.utils.openstack_tenant_backend'
    )
    def test_sync_instances_if_tenant_has_been_synchronized(self, mock_backend):
        mock_backend.OpenStackTenantBackend().get_importable_instances.return_value = [
            {'backend_id': self.instance.backend_id}
        ]
        mock_backend.OpenStackTenantBackend().import_instance.return_value = (
            self.instance
        )
        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=self.instance).exists()
        )
        resource = marketplace_models.Resource.objects.get(scope=self.instance)

        # deleting of expired instance
        mock_backend.OpenStackTenantBackend().get_importable_instances.return_value = []
        mock_backend.OpenStackTenantBackend().get_expired_instances.return_value = [
            self.instance
        ]
        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )
        resource.refresh_from_db()
        self.assertEqual(resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(
            openstack_tenant_models.Instance.DoesNotExist, self.instance.refresh_from_db
        )
