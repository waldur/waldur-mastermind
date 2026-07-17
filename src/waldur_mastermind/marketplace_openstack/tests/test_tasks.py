import unittest
from unittest import mock

from waldur_core.core import utils as core_utils
from waldur_core.logging import models as logging_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    OrderStates,
    OrderTypes,
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


class TerminateChildResourcesOfTerminatedTenantsTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.instance = self.fixture.instance
        self.volume = self.fixture.volume

        # Parent tenant offering + its (already) TERMINATED marketplace resource.
        self.tenant_offering = marketplace_factories.OfferingFactory(
            scope=self.tenant, type=OPENSTACK_TENANT_OFFERING
        )
        self.tenant_resource = marketplace_factories.ResourceFactory(
            offering=self.tenant_offering,
            scope=self.tenant,
            state=ResourceStates.TERMINATED,
        )

        # Per-tenant child offerings scoped to the tenant.
        self.instance_offering = marketplace_factories.OfferingFactory(
            scope=self.tenant, type=OPENSTACK_INSTANCE_OFFERING
        )
        self.volume_offering = marketplace_factories.OfferingFactory(
            scope=self.tenant, type=OPENSTACK_VOLUME_OFFERING
        )

    def test_orphaned_child_resources_are_terminated(self):
        instance_resource = marketplace_factories.ResourceFactory(
            offering=self.instance_offering,
            scope=self.instance,
            state=ResourceStates.OK,
        )
        volume_resource = marketplace_factories.ResourceFactory(
            offering=self.volume_offering,
            scope=self.volume,
            state=ResourceStates.ERRED,
        )

        tasks.terminate_child_resources_of_terminated_tenants()

        instance_resource.refresh_from_db()
        volume_resource.refresh_from_db()
        self.assertEqual(instance_resource.state, ResourceStates.TERMINATED)
        self.assertEqual(volume_resource.state, ResourceStates.TERMINATED)

    def test_a_completed_terminate_order_is_recorded_with_reason(self):
        instance_resource = marketplace_factories.ResourceFactory(
            offering=self.instance_offering,
            scope=self.instance,
            state=ResourceStates.OK,
        )

        tasks.terminate_child_resources_of_terminated_tenants()

        order = marketplace_models.Order.objects.get(
            resource=instance_resource, type=OrderTypes.TERMINATE
        )
        self.assertEqual(order.state, OrderStates.DONE)
        self.assertIsNone(order.created_by)
        self.assertIn("reason", order.attributes)

    def test_plugin_rows_are_not_deleted(self):
        marketplace_factories.ResourceFactory(
            offering=self.instance_offering,
            scope=self.instance,
            state=ResourceStates.OK,
        )

        tasks.terminate_child_resources_of_terminated_tenants()

        # Mark-only: the plugin Instance/Tenant rows must survive.
        self.instance.refresh_from_db()
        self.tenant.refresh_from_db()

    def test_child_resources_of_active_tenant_are_untouched(self):
        active_tenant = openstack_factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
        )
        active_tenant_offering = marketplace_factories.OfferingFactory(
            scope=active_tenant, type=OPENSTACK_TENANT_OFFERING
        )
        marketplace_factories.ResourceFactory(
            offering=active_tenant_offering,
            scope=active_tenant,
            state=ResourceStates.OK,
        )
        active_instance = openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=active_tenant,
        )
        active_instance_offering = marketplace_factories.OfferingFactory(
            scope=active_tenant, type=OPENSTACK_INSTANCE_OFFERING
        )
        active_resource = marketplace_factories.ResourceFactory(
            offering=active_instance_offering,
            scope=active_instance,
            state=ResourceStates.OK,
        )

        tasks.terminate_child_resources_of_terminated_tenants()

        active_resource.refresh_from_db()
        self.assertEqual(active_resource.state, ResourceStates.OK)

    def test_already_terminated_child_resources_are_left_as_is(self):
        terminated_resource = marketplace_factories.ResourceFactory(
            offering=self.instance_offering,
            scope=self.instance,
            state=ResourceStates.TERMINATED,
        )

        tasks.terminate_child_resources_of_terminated_tenants()

        terminated_resource.refresh_from_db()
        self.assertEqual(terminated_resource.state, ResourceStates.TERMINATED)

    def test_termination_is_recorded_in_the_audit_log(self):
        instance_resource = marketplace_factories.ResourceFactory(
            offering=self.instance_offering,
            scope=self.instance,
            state=ResourceStates.OK,
        )

        tasks.terminate_child_resources_of_terminated_tenants()

        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="marketplace_resource_terminate_succeeded",
                message__contains=instance_resource.name,
            ).exists()
        )

    @mock.patch(
        "waldur_mastermind.marketplace_openstack.tasks.marketplace_callbacks.resource_deletion_succeeded"
    )
    def test_failure_on_one_resource_is_isolated_and_rolled_back(self, mock_callback):
        mock_callback.side_effect = Exception("boom")
        instance_resource = marketplace_factories.ResourceFactory(
            offering=self.instance_offering,
            scope=self.instance,
            state=ResourceStates.OK,
        )

        # The task must swallow the per-resource error and not propagate it.
        tasks.terminate_child_resources_of_terminated_tenants()

        # The resource is left untouched and the order creation is rolled back
        # together with the failed callback (per-resource transaction.atomic).
        instance_resource.refresh_from_db()
        self.assertEqual(instance_resource.state, ResourceStates.OK)
        self.assertFalse(
            marketplace_models.Order.objects.filter(
                resource=instance_resource, type=OrderTypes.TERMINATE
            ).exists()
        )


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
