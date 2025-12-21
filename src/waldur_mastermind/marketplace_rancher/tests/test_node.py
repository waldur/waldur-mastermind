from typing import cast
from unittest import mock

from django.contrib.auth.models import AbstractBaseUser
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    RANCHER_OFFERING,
    BillingTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
)
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures
from waldur_rancher import models as rancher_models
from waldur_rancher.enums import AGENT_ROLE, SERVER_ROLE
from waldur_rancher.tests import factories as rancher_factories


@override_settings(
    task_always_eager=True,
)
class TestManagedRancherNodeCreate(APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        openstack_settings = self.fixture.settings

        # Create tenant objects
        self.tenant = self.fixture.tenant
        openstack_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=openstack_settings
        )
        marketplace_factories.ResourceFactory(
            scope=self.tenant,
            offering=openstack_offering,
            state=ResourceStates.OK,
            project=self.fixture.project,
        )
        openstack_factories.SecurityGroupFactory(tenant=self.tenant, name="default")
        for ct in [RAM_TYPE, CORES_TYPE, STORAGE_TYPE]:
            marketplace_factories.OfferingComponentFactory(
                offering=openstack_offering,
                type=ct,
                billing_type=BillingTypes.LIMIT,
            )
        self.flavor = self.fixture.flavor

        instance = self.fixture.instance
        self.volume = self.fixture.volume
        instance.volumes.add(self.volume)

        self.tenant.set_quota_limit("storage", self.volume.size)
        self.tenant.set_quota_limit("vcpu", self.flavor.cores * 2)
        self.tenant.set_quota_limit("ram", self.flavor.ram)

        self.tenant.set_quota_usage("storage", self.volume.size)
        self.tenant.set_quota_usage("vcpu", self.flavor.cores)
        self.tenant.set_quota_usage("ram", self.flavor.ram)

        # Create network objects
        subnet = self.fixture.subnet
        options = cast(dict, openstack_settings.options)
        options["base_subnet_name"] = subnet.name
        openstack_settings.save()

        # Create volumes
        self.volume_type = self.fixture.volume_type

        # Create image
        base_image = openstack_factories.ImageFactory(
            settings=openstack_settings, name="ubuntu-20.04"
        )
        base_image.tenants.add(self.tenant)
        base_image.save()
        rancher_service_settings = rancher_factories.RancherServiceSettingsFactory(
            options={"base_image_name": base_image.name}
        )
        rancher_offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING,
            scope=rancher_service_settings,
        )
        self.cluster = rancher_factories.ClusterFactory(
            service_settings=rancher_service_settings
        )
        self.cluster_resource = marketplace_factories.ResourceFactory(
            offering=rancher_offering,
            scope=self.cluster,
            project=self.fixture.project,
            limits={
                "storage": self.volume.size,
                "cores": self.flavor.cores,
                "ram": self.flavor.ram,
            },
            state=ResourceStates.OK,
        )
        # Create Rancher node
        rancher_factories.NodeFactory(
            instance=instance, cluster=self.cluster, role=SERVER_ROLE
        )

        self.url = (
            "http://testserver"
            + reverse(
                "managed-rancher-cluster-resource-detail",
                kwargs={"uuid": self.cluster_resource.uuid.hex},
            )
            + "add_node/"
        )

        self.payload = {
            "role": AGENT_ROLE,
            "subnet": openstack_factories.SubNetFactory.get_url(subnet),
            "flavor": openstack_factories.FlavorFactory.get_url(self.flavor),
            "system_volume_size": self.volume.size,
            "system_volume_type": openstack_factories.VolumeTypeFactory.get_url(
                self.volume_type
            ),
            "tenant": openstack_factories.TenantFactory.get_url(self.tenant),
            "data_volumes": [
                {
                    "volume_type": openstack_factories.VolumeTypeFactory.get_url(
                        self.volume_type
                    ),
                    "size": self.volume.size,
                    "mount_point": "/dev/sde",
                }
            ],
        }

        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

    @mock.patch("waldur_openstack.backend.OpenStackBackend.push_tenant_quotas")
    @mock.patch("waldur_rancher.tasks.CreateNodeTask.execute")
    @mock.patch("waldur_rancher.tasks.PollRuntimeStateNodeTask.execute")
    def test_managed_rancher_node_create(
        self,
        mock_poll_state_task_execute: mock.MagicMock,
        mock_create_node_task_execute: mock.MagicMock,
        mock_push_tenant_quotas: mock.MagicMock,
    ):
        mock_push_tenant_quotas.return_value = {}
        mock_poll_state_task_execute.return_value = {}
        mock_create_node_task_execute.return_value = {}

        self.client.force_login(cast(AbstractBaseUser, self.fixture.owner))
        new_limits = {
            "vcpu": self.flavor.cores * 2,
            "ram": self.flavor.ram * 2,
            "storage": self.volume.size * 3,
        }
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        mock_push_tenant_quotas.assert_called_once_with(self.tenant, new_limits)
        mock_create_node_task_execute.assert_called_once()
        mock_poll_state_task_execute.assert_called_once()

        self.assertTrue(
            rancher_models.Node.objects.filter(uuid=response.json()["uuid"]).exists()
        )
        new_node = rancher_models.Node.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(new_node.role, AGENT_ROLE)
        self.assertEqual(CoreStates.OK, new_node.state)

    def test_managed_rancher_node_creation_forbidden(self):
        self.client.force_login(cast(AbstractBaseUser, self.fixture.admin))
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
