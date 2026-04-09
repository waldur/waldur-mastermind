from typing import cast
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test
from rest_framework.response import Response

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests.factories import (
    ProjectFactory,
    SshPublicKeyFactory,
    UserFactory,
)
from waldur_mastermind.marketplace.enums import RANCHER_OFFERING, OrderTypes
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OrderFactory,
    PlanFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace.utils import process_order
from waldur_mastermind.marketplace_rancher.const import DEPLOYMENT_MODE_SELF_MANAGED
from waldur_openstack import models as openstack_models
from waldur_openstack.tests import (
    factories as openstack_factories,
)
from waldur_rancher import exceptions, models, tasks
from waldur_rancher.tests import factories, fixtures, utils


class ClusterGetTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture_2 = fixtures.RancherFixture()
        self.url = factories.ClusterFactory.get_list_url()

    def test_get_cluster_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_user_cannot_get_strangers_clusters(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_rancher_cluster_is_exposed_for_openstack_instance(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            openstack_factories.InstanceFactory.get_url(self.fixture.instance)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["rancher_cluster"]["uuid"], self.fixture.cluster.uuid.hex
        )

    def test_rancher_cluster_is_none_if_node_is_not_existed(self):
        self.fixture.node.delete()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            openstack_factories.InstanceFactory.get_url(self.fixture.instance)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["rancher_cluster"], None)

    def test_rancher_cluster_is_filtered_out_for_unrelated_user(self):
        project = ProjectFactory(customer=self.fixture.customer)
        admin = UserFactory()
        project.add_user(admin, ProjectRole.ADMIN)
        vm = openstack_factories.InstanceFactory(
            tenant=self.fixture.tenant,
            service_settings=self.fixture.tenant.service_settings,
            project=project,
            state=CoreStates.OK,
        )
        self.client.force_authenticate(admin)
        response = self.client.get(openstack_factories.InstanceFactory.get_url(vm))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["rancher_cluster"], None)


class BaseClusterCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = OrderFactory.get_list_url()
        self.tenant = self.fixture.tenant

        self.flavor = openstack_factories.FlavorFactory(
            settings=self.tenant.service_settings,
            ram=1024 * 8,
            cores=2,
        )
        self.flavor.tenants.add(self.tenant)
        image = openstack_factories.ImageFactory(settings=self.tenant.service_settings)
        image.tenants.add(self.fixture.tenant)
        self.default_security_group = openstack_factories.SecurityGroupFactory(
            name="default", tenant=self.tenant
        )
        self.fixture.settings.options["base_image_name"] = image.name
        self.fixture.settings.options["cloud_init_template"] = ""
        self.fixture.settings.save()

        self.network = openstack_factories.NetworkFactory(tenant=self.tenant)
        self.subnet = openstack_factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            project=self.fixture.project,
        )
        self.fixture.settings.options["base_subnet_name"] = self.subnet.name
        self.fixture.settings.save()

        self.offering = OfferingFactory(
            type=RANCHER_OFFERING,
            scope=self.fixture.settings,
        )
        self.offering.plugin_options["deployment_mode"] = DEPLOYMENT_MODE_SELF_MANAGED
        self.offering.save()
        self.plan = PlanFactory(offering=self.offering)


class ClusterCreateTest(BaseClusterCreateTest):
    def setUp(self):
        super().setUp()
        self.default_conf = {
            "subnet": openstack_factories.SubNetFactory.get_url(self.subnet),
            "system_volume_size": 1024,
            "flavor": openstack_factories.FlavorFactory.get_url(self.flavor),
        }
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.CREATE_ORDER)

    def tearDown(self):
        mock.patch.stopall()

    def _prepare_request(
        self,
        name,
        disk=1024,
        add_payload=None,
        install_longhorn=False,
        agent_count=1,
        server_count=3,
    ):
        add_payload = add_payload or {}
        default_conf = {
            "subnet": openstack_factories.SubNetFactory.get_url(self.subnet),
            "system_volume_size": disk,
            "flavor": openstack_factories.FlavorFactory.get_url(self.flavor),
        }
        attributes = {
            "name": name,
            "tenant": openstack_factories.TenantFactory.get_url(self.fixture.tenant),
            "nodes": utils.format_nodes(default_conf, server_count, agent_count),
            "install_longhorn": install_longhorn,
        }
        attributes.update(add_payload)

        payload = {
            "project": ProjectFactory.get_url(self.fixture.project),
            "offering": OfferingFactory.get_public_url(self.offering),
            "plan": PlanFactory.get_public_url(self.plan),
            "attributes": attributes,
        }
        return payload

    def _create_order_and_process(self, name, **kwargs):
        payload = self._prepare_request(name, **kwargs)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = Order.objects.get(uuid=response.data["uuid"])
        process_order(order, self.fixture.owner)
        order.refresh_from_db()
        return response, order

    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_create_cluster(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        self._create_order_and_process("new-cluster")
        self.assertTrue(models.Cluster.objects.filter(name="new-cluster").exists())
        cluster = models.Cluster.objects.get(name="new-cluster")
        mock_core_tasks.BackendMethodTask.return_value.si.assert_has_calls(
            [
                mock.call(
                    f"waldur_rancher.cluster:{cluster.id}",
                    "create_cluster",
                    state_transition="begin_creating",
                )
            ]
        )

    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_use_data_volumes(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        self.tenant.service_settings.shared = True
        self.tenant.service_settings.save()
        volume_type = openstack_factories.VolumeTypeFactory()
        volume_type.tenants.add(self.tenant)
        default_conf = {
            **self.default_conf,
            "data_volumes": [
                {
                    "size": 12 * 1024,
                    "volume_type": openstack_factories.VolumeTypeFactory.get_url(
                        volume_type
                    ),
                    "mount_point": "/var/lib/etcd",
                }
            ],
        }
        self._create_order_and_process(
            "new-cluster", add_payload={"nodes": utils.format_nodes(default_conf, 3, 1)}
        )
        self.assertTrue(models.Cluster.objects.filter(name="new-cluster").exists())
        cluster = models.Cluster.objects.get(name="new-cluster")
        self.assertEqual(len(cluster.node_set.first().initial_data["data_volumes"]), 1)

    def test_node_name_uniqueness(self):
        self.client.force_authenticate(self.fixture.owner)
        self._create_order_and_process(
            "new-cluster",
            add_payload={"nodes": utils.format_nodes(self.default_conf, 3, 1)},
        )
        self.assertTrue(models.Cluster.objects.filter(name="new-cluster").exists())
        cluster = models.Cluster.objects.get(name="new-cluster")
        self.assertNotEqual(
            cluster.node_set.all()[0].name, cluster.node_set.all()[1].name
        )

    def test_validate_server_node_count(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._prepare_request(
            "new-cluster",
            add_payload={"nodes": utils.format_nodes(self.default_conf, 2, 1)},
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            "Total count of server nodes must be 1, 3 or 5."
            in response.data["nodes"][0]
        )

    def test_validate_agent_node_count(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._prepare_request(
            "new-cluster",
            add_payload={"nodes": utils.format_nodes(self.default_conf, 3, 0)},
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            "Count of agent nodes must be min 1." in response.data["nodes"][0]
        )

    def test_validate_name_uniqueness(self):
        self.client.force_authenticate(self.fixture.owner)
        response, order = self._create_order_and_process("new-cluster")
        payload = self._prepare_request("new-cluster")
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_name(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._prepare_request("new_cluster")
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def mock_backend(self):
        mock_token_patch = mock.patch(
            "waldur_rancher.client.RancherClient.create_cluster_registration_token"
        )
        mock_token_patch.start()
        mock_backend_patch = mock.patch(
            "waldur_rancher.backend.RancherBackend._backend_cluster_to_cluster"
        )
        mock_backend_patch.start()
        mock_v1_to_v3_patch = mock.patch(
            "waldur_rancher.client.RancherClient.get_v3_cluster_id"
        )
        mock_v1_to_v3 = mock_v1_to_v3_patch.start()
        mock_v1_to_v3.return_value = "v3_cluster_id"

    @mock.patch("waldur_rancher.client.RancherClient._post")
    def test_create_private_cluster(self, mock_client_post):
        self.mock_backend()
        private_registry_credentials_secret_name = "registryconfig-auth-abc"
        mock_client_post.side_effect = [
            None,
            {"metadata": {"name": private_registry_credentials_secret_name}},
            {"id": "test-id"},
        ]
        options = cast(dict, self.fixture.settings.options)
        k8s_version = "v1.33.0+rke2r1"
        options.update(
            {
                "private_registry_url": "example.com",
                "private_registry_user": "user",
                "private_registry_password": "1234",
                "k8s_version": k8s_version,
            }
        )
        self.fixture.settings.save()
        self.fixture.cluster.backend_id = ""
        self.fixture.cluster.save()
        backend = self.fixture.cluster.get_backend()
        backend.create_cluster(self.fixture.cluster)
        self.assertEqual(
            mock_client_post.call_args_list[1][1]["json"],
            {
                "type": "kubernetes.io/basic-auth",
                "metadata": {
                    "namespace": "fleet-default",
                    "generateName": "registryconfig-auth-",
                },
                "data": {"username": "dXNlcg==", "password": "MTIzNA=="},
            },
        )
        self.assertEqual(
            mock_client_post.call_args_list[2][1]["json"],
            {
                "type": "provisioning.cattle.io.cluster",
                "metadata": {
                    "name": self.fixture.cluster.name,
                    "namespace": "fleet-default",
                },
                "spec": {
                    "rkeConfig": {
                        "chartValues": {"rke2-cilium": {}},
                        "dataDirectories": {
                            "systemAgent": "/opt/rke2_storage/agent",
                            "provisioning": "/opt/rke2_storage/provisioning",
                            "k8sDistro": "/opt/rke2_storage/rke2",
                        },
                        "machineGlobalConfig": {
                            "cni": "cilium",
                            "disable-kube-proxy": False,
                            "etcd-expose-metrics": False,
                        },
                        "machineSelectorConfig": [
                            {
                                "config": {
                                    "protect-kernel-defaults": False,
                                    "system-default-registry": "example.com",
                                }
                            }
                        ],
                        "registries": {
                            "configs": {
                                "example.com": {
                                    "authConfigSecretName": private_registry_credentials_secret_name,
                                    "caBundle": None,
                                    "insecureSkipVerify": False,
                                    "tlsSecretName": None,
                                }
                            },
                            "mirrors": {},
                        },
                    },
                    "kubernetesVersion": k8s_version,
                },
            },
        )

    @mock.patch("waldur_rancher.tasks.common_utils")
    @mock.patch("waldur_rancher.tasks.reverse")
    def test_create_cluster_with_nodes_with_floating_ips(
        self, mock_reverse, mock_common_utils
    ):
        self.fixture.settings.options["allocate_floating_ip_to_all_nodes"] = True
        self.fixture.settings.save()
        self.fixture.node.initial_data = {
            "flavor": "",
            "vcpu": "",
            "ram": "",
            "image": "",
            "subnet": "",
            "service_settings": "",
            "tenant": "",
            "project": "",
            "system_volume_size": "",
            "system_volume_type": "",
            "data_volumes": [],
            "security_groups": [],
        }
        self.fixture.node.save()
        try:
            tasks.CreateNodeTask().execute(self.fixture.node, self.fixture.staff.id)
        except exceptions.RancherException:
            self.assertTrue(
                "floating_ips" in mock_common_utils.create_request.call_args[0][2]
            )

    @mock.patch("waldur_rancher.executors.core_tasks")
    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_create_is_disabled_in_read_only_mode(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._prepare_request("new-cluster")
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_use_ssh_public_key(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        ssh_public_key = SshPublicKeyFactory(user=self.fixture.owner)
        payload = {
            "ssh_public_key": SshPublicKeyFactory.get_url(ssh_public_key),
        }
        self._create_order_and_process("new-cluster", add_payload=payload)
        cluster = models.Cluster.objects.get(name="new-cluster")
        self.assertEqual(
            cluster.node_set.first().initial_data["ssh_public_key"],
            ssh_public_key.uuid.hex,
        )

    def test_validate_security_groups_positive(self):
        security_group1 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        security_group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "security_groups": [
                {
                    "url": openstack_factories.SecurityGroupFactory.get_url(
                        security_group1
                    )
                },
                {
                    "url": openstack_factories.SecurityGroupFactory.get_url(
                        security_group2
                    )
                },
            ]
        }
        self._create_order_and_process("new-cluster", add_payload=payload)

    def test_validate_security_groups_negative(self):
        security_group1 = openstack_factories.SecurityGroupFactory()
        security_group2 = openstack_factories.SecurityGroupFactory()
        self.client.force_authenticate(self.fixture.owner)
        payload = {
            "security_groups": [
                {
                    "url": openstack_factories.SecurityGroupFactory.get_url(
                        security_group1
                    )
                },
                {
                    "url": openstack_factories.SecurityGroupFactory.get_url(
                        security_group2
                    )
                },
            ]
        }
        payload = self._prepare_request(name="new-cluster", add_payload=payload)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_default_security_groups_is_used_if_custom_is_not_provided(self):
        self.client.force_authenticate(self.fixture.owner)
        self._create_order_and_process("new-cluster")
        cluster = models.Cluster.objects.get(name="new-cluster")
        self.assertEqual(
            cluster.node_set.first().initial_data["security_groups"],
            [self.default_security_group.uuid.hex],
        )

    def test_vm_project_is_saved_in_vm_spec(self):
        self.client.force_authenticate(self.fixture.owner)
        project = ProjectFactory(customer=self.fixture.customer)
        response, order = self._create_order_and_process(
            "new-cluster", add_payload={"vm_project": ProjectFactory.get_url(project)}
        )
        cluster = models.Cluster.objects.get(name="new-cluster")
        node = cluster.node_set.first()
        self.assertEqual(cluster.vm_project, project)
        self.assertEqual(
            node.initial_data["project"],
            project.uuid.hex,
        )

    def test_when_both_node_and_cluster_tenant_are_specified_error_is_raised(self):
        self.client.force_authenticate(self.fixture.owner)
        nodes = utils.format_nodes(self.default_conf, 3, 1)
        for node in nodes:
            node["tenant"] = openstack_factories.TenantFactory.get_url(
                self.fixture.tenant
            )
        request = self._prepare_request(
            "new-cluster",
            add_payload={"nodes": nodes},
        )
        response = self.client.post(self.url, request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_when_both_node_and_cluster_tenant_are_not_specified_error_is_raised(self):
        self.client.force_authenticate(self.fixture.owner)
        nodes = utils.format_nodes(self.default_conf, 3, 1)
        request = self._prepare_request(
            "new-cluster",
            add_payload={"nodes": nodes},
        )
        del request["attributes"]["tenant"]
        response = self.client.post(self.url, request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_ok_if_only_node_tenant_is_specified(self):
        self.client.force_authenticate(self.fixture.owner)
        nodes = utils.format_nodes(self.default_conf, 3, 1)
        for node in nodes:
            node["tenant"] = openstack_factories.TenantFactory.get_url(
                self.fixture.tenant
            )
        request = self._prepare_request(
            "new-cluster",
            add_payload={"nodes": nodes},
        )
        del request["attributes"]["tenant"]
        response = self.client.post(self.url, request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get(uuid=response.data["uuid"])
        process_order(order, self.fixture.owner)
        cluster = models.Cluster.objects.get(name="new-cluster")
        node = cluster.node_set.first()
        self.assertEqual(
            node.initial_data["tenant"],
            self.fixture.tenant.uuid.hex,
        )

    def test_custom_security_groups_are_propagated_to_initial_data(self):
        security_group1 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        security_group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        self.client.force_authenticate(self.fixture.owner)
        payload = {
            "security_groups": [
                {
                    "url": openstack_factories.SecurityGroupFactory.get_url(
                        security_group1
                    )
                },
                {
                    "url": openstack_factories.SecurityGroupFactory.get_url(
                        security_group2
                    )
                },
            ]
        }
        self._create_order_and_process("new-cluster", add_payload=payload)

        cluster = models.Cluster.objects.get(name="new-cluster")
        self.assertEqual(
            cluster.node_set.first().initial_data["security_groups"],
            [security_group1.uuid.hex, security_group2.uuid.hex],
        )

    @utils.override_plugin_settings(DISABLE_SSH_KEY_INJECTION=True)
    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_disable_ssh_public_key(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        ssh_public_key = SshPublicKeyFactory(user=self.fixture.owner)
        payload = {
            "ssh_public_key": SshPublicKeyFactory.get_url(ssh_public_key),
        }
        self._create_order_and_process("new-cluster", add_payload=payload)
        cluster = models.Cluster.objects.get(name="new-cluster")
        self.assertTrue("ssh_public_key" not in cluster.node_set.first().initial_data)

    @utils.override_plugin_settings(DISABLE_DATA_VOLUME_CREATION=True)
    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_disable_data_volumes(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        volume_type = openstack_factories.VolumeTypeFactory(
            settings=self.tenant.service_settings
        )
        volume_type.tenants.add(self.tenant)
        default_conf = {
            **self.default_conf,
            "data_volumes": [
                {
                    "size": 12 * 1024,
                    "volume_type": openstack_factories.VolumeTypeFactory.get_url(
                        volume_type
                    ),
                    "mount_point": "/var/lib/etcd",
                }
            ],
        }
        self._create_order_and_process(
            "new-cluster", add_payload={"nodes": utils.format_nodes(default_conf, 3, 1)}
        )
        self.assertTrue(models.Cluster.objects.filter(name="new-cluster").exists())
        cluster = models.Cluster.objects.get(name="new-cluster")
        self.assertEqual(len(cluster.node_set.first().initial_data["data_volumes"]), 0)


@ddt
class ClusterGroupCreateTest(BaseClusterCreateTest):
    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        self.url = factories.ClusterFactory.get_url(
            cluster=self.fixture.cluster, action="create_management_security_group"
        )

    @data("staff", "owner", "admin", "manager")
    def test_create_management_security_group(self, user):
        tenant = openstack_factories.TenantFactory(project=self.fixture.project)
        self.fixture.settings.options["management_tenant_uuid"] = tenant.uuid.hex
        self.fixture.settings.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.fixture.cluster.refresh_from_db()
        self.assertTrue(self.fixture.cluster.management_security_group)
        group_uuid = response.data["security_group_uuid"]
        group = openstack_models.SecurityGroup.objects.get(uuid=group_uuid)
        self.assertEqual(
            group.rules.first().direction, openstack_models.SecurityGroupRule.INGRESS
        )
        self.assertEqual(
            group.rules.first().ethertype, openstack_models.SecurityGroupRule.IPv4
        )
        self.assertEqual(group.rules.first().cidr, "192.168.77.0/24")
        self.assertEqual(group.rules.first().to_port, 443)
        self.assertEqual(group.rules.first().from_port, 443)

    def test_group_creating_is_not_available_if_management_tenant_is_not_set(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, self.get_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue("Management tenant is not set." in response.data)

    def get_payload(self):
        return [{"cidr": "192.168.77.0/24"}]


class ClusterPullTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = factories.ClusterFactory.get_url(self.fixture.cluster, action="pull")

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_pull_is_enabled_for_staff_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_pull_is_disabled_for_owner_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_pull_is_enabled_for_owner_when_read_only_mode_is_disabled(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)


class ClusterUpdateTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.cluster_name = self.fixture.cluster.name
        self.url = factories.ClusterFactory.get_url(self.fixture.cluster)

    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_send_backend_request_if_update_cluster_name(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {"name": "new-name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_core_tasks.BackendMethodTask.return_value.si.assert_called_once_with(
            f"waldur_rancher.cluster:{self.fixture.cluster.id}",
            "update_cluster",
            state_transition="begin_updating",
        )

    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_not_send_backend_request_if_update_cluster_description(
        self, mock_core_tasks
    ):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {"description": "description"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_core_tasks.StateTransitionTask.return_value.si.assert_called_once_with(
            f"waldur_rancher.cluster:{self.fixture.cluster.id}",
            state_transition="begin_updating",
        )

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_update_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {"name": "new-name"})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class ClusterDeleteTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.cluster = self.fixture.cluster
        self.cluster.state = CoreStates.OK
        self.cluster.save()
        self.resource = ResourceFactory(
            project=self.fixture.project,
            offering=OfferingFactory(type=RANCHER_OFFERING),
            state=Resource.States.OK,
            scope=self.cluster,
        )

        self.url = ResourceFactory.get_url(self.resource, action="terminate")
        CustomerRole.OWNER.add_permission(PermissionEnum.TERMINATE_RESOURCE)

    @mock.patch("waldur_rancher.executors.core_tasks")
    def test_delete_cluster_if_related_nodes_do_not_exist(self, mock_core_tasks):
        self.fixture.node.delete()
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = Order.objects.get(resource=self.resource, type=OrderTypes.TERMINATE)
        process_order(order, self.fixture.owner)

        mock_core_tasks.BackendMethodTask.return_value.si.assert_called_once_with(
            f"waldur_rancher.cluster:{self.cluster.id}",
            "delete_cluster",
            state_transition="begin_deleting",
        )

    def test_not_delete_cluster_if_state_is_not_ok(self):
        self.client.force_authenticate(self.fixture.owner)
        self.cluster.state = CoreStates.CREATION_SCHEDULED
        self.cluster.save()
        self.resource.state = Resource.States.CREATING
        self.resource.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch("waldur_rancher.executors.chain")
    @mock.patch("waldur_rancher.executors.tasks")
    def test_when_cluster_is_deleted_node_deletion_is_requested(
        self, mock_tasks, mock_chain
    ):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order = Order.objects.get(resource=self.resource, type=OrderTypes.TERMINATE)
        process_order(order, self.fixture.owner)

        mock_tasks.DeleteNodeTask.return_value.si.assert_called_once_with(
            f"waldur_rancher.node:{self.fixture.node.id}",
            user_id=self.fixture.owner.id,
        )

    @mock.patch("waldur_rancher.executors.chain")
    @mock.patch("waldur_rancher.executors.tasks")
    def test_when_there_are_no_valid_nodes_task_is_not_called(
        self, mock_tasks, mock_chain
    ):
        self.fixture.node.backend_id = ""
        self.fixture.node.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = Order.objects.get(resource=self.resource, type=OrderTypes.TERMINATE)
        process_order(order, self.fixture.owner)

        self.assertEqual(mock_tasks.DeleteNodeTask.return_value.si.call_count, 0)

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    @mock.patch("waldur_rancher.tasks.common_utils.delete_request")
    def test_when_cluster_is_deleted_instance_deletion_is_requested(
        self, mock_delete_request, mock_client
    ):
        mock_delete_request.return_value = Response(status=status.HTTP_202_ACCEPTED)
        mock_client.get_node.return_value = {
            "conditions": [{"type": "Drained", "status": "True"}]
        }
        tasks.DeleteNodeTask().execute(self.fixture.node, user_id=self.fixture.owner.id)
        vm = self.fixture.node.instance
        self.assertEqual(mock_delete_request.call_count, 1)
        self.assertEqual(mock_delete_request.call_args[0][1], self.fixture.owner)
        self.assertEqual(
            mock_delete_request.call_args[1],
            {
                "uuid": vm.uuid.hex,
                "query_params": {"delete_volumes": True},
            },
        )
        mock_client.drain_node.assert_called_once_with(self.fixture.node.backend_id)

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_if_instance_has_been_deleted_node_and_cluster_are_deleted(
        self, mock_client
    ):
        self.fixture.cluster.state = CoreStates.DELETING
        self.fixture.cluster.save()
        self.fixture.node.backend_id = "backend_id"
        self.fixture.node.save()
        self.fixture.instance.delete()
        self.assertRaises(
            models.Cluster.DoesNotExist, self.fixture.cluster.refresh_from_db
        )
        self.assertRaises(models.Node.DoesNotExist, self.fixture.node.refresh_from_db)
        mock_client.delete_cluster.assert_called_once_with(
            self.fixture.cluster.backend_id
        )


@ddt
class ClusterSecurityGroupRulesTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.security_group = factories.ClusterSecurityGroupFactory(
            cluster=self.fixture.cluster
        )
        self.fixture.node
        openstack_factories.SecurityGroupFactory(
            tenant=self.fixture.tenant, name=self.security_group.name
        )
        self.url = factories.ClusterSecurityGroupFactory.get_url(self.security_group)

    @data("staff", "owner", "admin", "manager")
    def test_user_can_update_security_group_rules(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "rules": [
                {
                    "direction": "ingress",
                    "protocol": "tcp",
                    "from_port": 443,
                    "to_port": 443,
                    "cidr": "192.168.77.0/24",
                }
            ]
        }
        response = self.client.put(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.security_group.refresh_from_db()
        rule = models.ClusterSecurityGroupRule.objects.filter(
            group=self.security_group
        ).get()
        self.assertEqual(rule.direction, "ingress")
        self.assertEqual(rule.protocol, "tcp")
        self.assertEqual(rule.from_port, 443)
        self.assertEqual(rule.to_port, 443)
        self.assertEqual(rule.cidr, "192.168.77.0/24")

    def test_validation_invalid_cidr(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "rules": [
                {
                    "direction": "ingress",
                    "protocol": "tcp",
                    "from_port": 443,
                    "to_port": 443,
                    "cidr": "invalid-cidr",
                }
            ]
        }
        response = self.client.put(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validation_missing_fields(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = [{"direction": "ingress"}]
        response = self.client.put(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rules_are_replaced(self):
        factories.ClusterSecurityGroupFactory(cluster=self.fixture.cluster)
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "rules": [
                {
                    "direction": "ingress",
                    "protocol": "tcp",
                    "from_port": 443,
                    "to_port": 443,
                    "cidr": "192.168.77.0/24",
                }
            ]
        }
        response = self.client.put(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            models.ClusterSecurityGroupRule.objects.filter(
                group=self.security_group
            ).count(),
            1,
        )

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_update_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "rules": [
                {
                    "direction": "ingress",
                    "protocol": "tcp",
                    "from_port": 443,
                    "to_port": 443,
                    "cidr": "192.168.77.0/24",
                }
            ]
        }
        response = self.client.put(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_validation_port_range(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "rules": [
                {
                    "direction": "ingress",
                    "protocol": "tcp",
                    "from_port": 443,
                    "to_port": 80,
                    "cidr": "192.168.77.0/24",
                }
            ]
        }
        response = self.client.put(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
