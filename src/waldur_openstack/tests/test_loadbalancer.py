from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack import models

from . import factories, fixtures


class BaseLoadBalancerTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)


class LoadBalancerListTest(BaseLoadBalancerTest):
    def test_list_load_balancers(self):
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.get(factories.LoadBalancerFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], lb.name)
        self.assertEqual(
            response.data[0]["vip_subnet"],
            factories.SubNetFactory.get_url(lb.vip_subnet),
        )


class LoadBalancerCreateTest(BaseLoadBalancerTest):
    @mock.patch("waldur_openstack.executors.LoadBalancerCreateExecutor.execute")
    def test_create_load_balancer(self, mock_execute):
        valid_data = {
            "name": "Test LB",
            "tenant": factories.TenantFactory.get_url(self.fixture.tenant),
            "vip_subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
        }
        response = self.client.post(
            factories.LoadBalancerFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_execute.assert_called_once()

    def test_create_requires_vip_subnet(self):
        valid_data = {
            "name": "Test LB",
            "tenant": factories.TenantFactory.get_url(self.fixture.tenant),
        }
        response = self.client.post(
            factories.LoadBalancerFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class LoadBalancerDeleteTest(BaseLoadBalancerTest):
    @mock.patch("waldur_openstack.executors.LoadBalancerDeleteExecutor.execute")
    def test_delete_load_balancer(self, mock_execute):
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.delete(factories.LoadBalancerFactory.get_url(lb))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()


class LoadBalancerUnlinkTest(BaseLoadBalancerTest):
    def setUp(self):
        super().setUp()
        self.lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.url = factories.LoadBalancerFactory.get_url(self.lb, action="unlink")

    @mock.patch("waldur_openstack.executors.LoadBalancerDeleteExecutor.execute")
    def test_staff_unlink_deletes_from_db_without_executor(self, mock_delete_execute):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.LoadBalancer.objects.filter(pk=self.lb.pk).exists())
        mock_delete_execute.assert_not_called()

    def test_non_staff_cannot_unlink(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.LoadBalancer.objects.filter(pk=self.lb.pk).exists())


class LoadBalancerUpdateTest(BaseLoadBalancerTest):
    @mock.patch("waldur_openstack.executors.LoadBalancerUpdateExecutor.execute")
    def test_update_load_balancer_name(self, mock_execute):
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.patch(
            factories.LoadBalancerFactory.get_url(lb),
            {"name": "Updated LB Name"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_execute.assert_called_once()


class LoadBalancerAttachFloatingIPTest(BaseLoadBalancerTest):
    @mock.patch(
        "waldur_openstack.executors.LoadBalancerAttachFloatingIPExecutor.execute"
    )
    def test_attach_floating_ip(self, mock_execute):
        vip_port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="vip_port_123",
        )
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            vip_port=vip_port,
            state=CoreStates.OK,
        )
        fip = factories.FloatingIPFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.post(
            factories.LoadBalancerFactory.get_url(lb, "attach_floating_ip"),
            {"floating_ip": factories.FloatingIPFactory.get_url(fip)},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_attach_fails_when_no_vip_port(self):
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            vip_port=None,
            state=CoreStates.OK,
        )
        fip = factories.FloatingIPFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.post(
            factories.LoadBalancerFactory.get_url(lb, "attach_floating_ip"),
            {"floating_ip": factories.FloatingIPFactory.get_url(fip)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("VIP port", str(response.data))


class LoadBalancerDetachFloatingIPTest(BaseLoadBalancerTest):
    @mock.patch(
        "waldur_openstack.executors.LoadBalancerDetachFloatingIPExecutor.execute"
    )
    def test_detach_floating_ip(self, mock_execute):
        fip = factories.FloatingIPFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            attached_floating_ip=fip,
            state=CoreStates.OK,
        )
        response = self.client.post(
            factories.LoadBalancerFactory.get_url(lb, "detach_floating_ip"),
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_detach_fails_when_no_floating_ip(self):
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
        )
        response = self.client.post(
            factories.LoadBalancerFactory.get_url(lb, "detach_floating_ip"),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no floating IP", str(response.data))


class LoadBalancerSetSecurityGroupsTest(BaseLoadBalancerTest):
    def setUp(self):
        super().setUp()
        self.vip_port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="vip_port_123",
        )
        self.lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            vip_port=self.vip_port,
            state=CoreStates.OK,
        )
        self.sg = factories.SecurityGroupFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.url = factories.LoadBalancerFactory.get_url(self.lb, "set_security_groups")

    @mock.patch(
        "waldur_openstack.executors.LoadBalancerSetSecurityGroupsExecutor.execute"
    )
    def test_set_security_groups(self, mock_execute):
        response = self.client.post(
            self.url,
            {"security_groups": [factories.SecurityGroupFactory.get_url(self.sg)]},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_set_security_groups_fails_when_no_vip_port(self):
        self.lb.vip_port = None
        self.lb.save()
        response = self.client.post(
            self.url,
            {"security_groups": [factories.SecurityGroupFactory.get_url(self.sg)]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("VIP port", str(response.data))

    def test_set_security_groups_fails_when_different_tenant(self):
        other_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
        )
        other_sg = factories.SecurityGroupFactory(
            tenant=other_tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.post(
            self.url,
            {"security_groups": [factories.SecurityGroupFactory.get_url(other_sg)]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("same tenant", str(response.data))

    @mock.patch(
        "waldur_openstack.executors.LoadBalancerSetSecurityGroupsExecutor.execute"
    )
    def test_set_empty_security_groups_clears_them(self, mock_execute):
        response = self.client.post(
            self.url,
            {"security_groups": []},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_set_security_groups_fails_when_lb_not_ok(self):
        self.lb.state = CoreStates.CREATING
        self.lb.save()
        response = self.client.post(
            self.url,
            {"security_groups": [factories.SecurityGroupFactory.get_url(self.sg)]},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_load_balancer_response_includes_vip_security_groups(self):
        # Directly set SGs on the port M2M to simulate what the backend does
        self.vip_port.security_groups.add(self.sg)
        response = self.client.get(factories.LoadBalancerFactory.get_url(self.lb))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["vip_security_groups"]), 1)
        self.assertEqual(
            response.data["vip_security_groups"][0]["uuid"],
            str(self.sg.uuid),
        )

    def test_load_balancer_response_vip_security_groups_empty_when_no_port(self):
        self.lb.vip_port = None
        self.lb.save()
        response = self.client.get(factories.LoadBalancerFactory.get_url(self.lb))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["vip_security_groups"], [])


class BasePoolTest(BaseLoadBalancerTest):
    def setUp(self) -> None:
        super().setUp()
        self.load_balancer = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="lb_backend_123",
            state=CoreStates.OK,
        )


class PoolListTest(BasePoolTest):
    def test_list_pools(self):
        pool = factories.PoolFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.get(factories.PoolFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], pool.name)
        self.assertEqual(response.data[0]["protocol"], pool.protocol)


class PoolCreateTest(BasePoolTest):
    @mock.patch("waldur_openstack.executors.PoolCreateExecutor.execute")
    def test_create_pool(self, mock_execute):
        valid_data = {
            "name": "Test Pool",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
        }
        response = self.client.post(factories.PoolFactory.get_list_url(), valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.PoolCreateExecutor.execute")
    def test_create_pool_with_source_ip_port_algorithm_succeeds(self, mock_execute):
        valid_data = {
            "name": "Test Pool",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
            "lb_algorithm": "SOURCE_IP_PORT",
        }
        response = self.client.post(factories.PoolFactory.get_list_url(), valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_execute.assert_called_once()

    def test_create_pool_with_unsupported_algorithm_for_ovn_provider_fails(self):
        valid_data = {
            "name": "Test Pool",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
            "lb_algorithm": "ROUND_ROBIN",
        }
        response = self.client.post(factories.PoolFactory.get_list_url(), valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("lb_algorithm", response.data)

    def test_create_fails_when_load_balancer_not_provisioned(self):
        self.load_balancer.backend_id = None
        self.load_balancer.save()
        valid_data = {
            "name": "Test Pool",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
        }
        response = self.client.post(factories.PoolFactory.get_list_url(), valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be provisioned", str(response.data))


class PoolDeleteTest(BasePoolTest):
    @mock.patch("waldur_openstack.executors.PoolDeleteExecutor.execute")
    def test_delete_pool(self, mock_execute):
        pool = factories.PoolFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.delete(factories.PoolFactory.get_url(pool))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()


class PoolUpdateTest(BasePoolTest):
    @mock.patch("waldur_openstack.executors.PoolUpdateExecutor.execute")
    def test_update_pool_name(self, mock_execute):
        pool = factories.PoolFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.patch(
            factories.PoolFactory.get_url(pool),
            {"name": "Updated Pool Name"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_execute.assert_called_once()


class BasePoolMemberTest(BaseLoadBalancerTest):
    def setUp(self) -> None:
        super().setUp()
        self.pool = factories.PoolFactory(
            load_balancer=factories.LoadBalancerFactory(
                tenant=self.fixture.tenant,
                project=self.fixture.project,
                service_settings=self.fixture.settings,
                backend_id="lb_backend_123",
                state=CoreStates.OK,
            ),
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="pool_backend_123",
            state=CoreStates.OK,
        )


class PoolMemberListTest(BasePoolMemberTest):
    def test_list_pool_members(self):
        member = factories.PoolMemberFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.get(factories.PoolMemberFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], member.name)
        self.assertEqual(response.data[0]["address"], str(member.address))
        self.assertEqual(response.data[0]["protocol_port"], member.protocol_port)


class PoolMemberCreateTest(BasePoolMemberTest):
    @mock.patch("waldur_openstack.executors.PoolMemberCreateExecutor.execute")
    def test_create_pool_member(self, mock_execute):
        valid_data = {
            "pool": factories.PoolFactory.get_url(self.pool),
            "address": "192.168.1.10",
            "protocol_port": 80,
            "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
        }
        response = self.client.post(
            factories.PoolMemberFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_execute.assert_called_once()

    def test_create_fails_when_pool_not_provisioned(self):
        self.pool.backend_id = None
        self.pool.save()
        valid_data = {
            "pool": factories.PoolFactory.get_url(self.pool),
            "address": "192.168.1.10",
            "protocol_port": 80,
            "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
        }
        response = self.client.post(
            factories.PoolMemberFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be provisioned", str(response.data))

    def test_create_requires_subnet(self):
        valid_data = {
            "pool": factories.PoolFactory.get_url(self.pool),
            "address": "192.168.1.10",
            "protocol_port": 80,
        }
        response = self.client.post(
            factories.PoolMemberFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_rejects_subnet_from_other_tenant(self):
        other_tenant = factories.TenantFactory(service_settings=self.fixture.settings)
        foreign_network = factories.NetworkFactory(
            tenant=other_tenant,
            project=other_tenant.project,
            service_settings=self.fixture.settings,
        )
        foreign_subnet = factories.SubNetFactory(
            network=foreign_network,
            tenant=other_tenant,
            project=other_tenant.project,
            service_settings=self.fixture.settings,
            backend_id="foreign_subnet_backend_id",
        )
        valid_data = {
            "pool": factories.PoolFactory.get_url(self.pool),
            "address": "192.168.1.10",
            "protocol_port": 80,
            "subnet": factories.SubNetFactory.get_url(foreign_subnet),
        }
        response = self.client.post(
            factories.PoolMemberFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PoolMemberDeleteTest(BasePoolMemberTest):
    @mock.patch("waldur_openstack.executors.PoolMemberDeleteExecutor.execute")
    def test_delete_pool_member(self, mock_execute):
        member = factories.PoolMemberFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.delete(factories.PoolMemberFactory.get_url(member))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()


class PoolMemberUpdateTest(BasePoolMemberTest):
    @mock.patch("waldur_openstack.executors.PoolMemberUpdateExecutor.execute")
    def test_update_pool_member_name(self, mock_execute):
        member = factories.PoolMemberFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.patch(
            factories.PoolMemberFactory.get_url(member),
            {"name": "Updated Member Name"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_execute.assert_called_once()


class BaseHealthMonitorTest(BaseLoadBalancerTest):
    def setUp(self) -> None:
        super().setUp()
        self.pool = factories.PoolFactory(
            load_balancer=factories.LoadBalancerFactory(
                tenant=self.fixture.tenant,
                project=self.fixture.project,
                service_settings=self.fixture.settings,
                backend_id="lb_backend_123",
                state=CoreStates.OK,
            ),
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="pool_backend_123",
            state=CoreStates.OK,
        )


class HealthMonitorListTest(BaseHealthMonitorTest):
    def test_list_health_monitors(self):
        hm = factories.HealthMonitorFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.get(factories.HealthMonitorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], hm.name)
        self.assertEqual(response.data[0]["type"], hm.monitor_type)
        self.assertEqual(response.data[0]["delay"], hm.delay)


class HealthMonitorCreateTest(BaseHealthMonitorTest):
    @mock.patch("waldur_openstack.executors.HealthMonitorCreateExecutor.execute")
    def test_create_health_monitor(self, mock_execute):
        valid_data = {
            "pool": factories.PoolFactory.get_url(self.pool),
            "type": "TCP",
            "delay": 10,
            "timeout": 5,
            "max_retries": 3,
        }
        response = self.client.post(
            factories.HealthMonitorFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_execute.assert_called_once()

    def test_create_fails_when_pool_not_provisioned(self):
        self.pool.backend_id = None
        self.pool.save()
        valid_data = {
            "pool": factories.PoolFactory.get_url(self.pool),
            "type": "TCP",
            "delay": 10,
            "timeout": 5,
            "max_retries": 3,
        }
        response = self.client.post(
            factories.HealthMonitorFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be provisioned", str(response.data))

    def test_create_fails_when_pool_already_has_health_monitor(self):
        factories.HealthMonitorFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        valid_data = {
            "pool": factories.PoolFactory.get_url(self.pool),
            "type": "TCP",
            "delay": 10,
            "timeout": 5,
            "max_retries": 3,
        }
        response = self.client.post(
            factories.HealthMonitorFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", str(response.data))


class HealthMonitorDeleteTest(BaseHealthMonitorTest):
    @mock.patch("waldur_openstack.executors.HealthMonitorDeleteExecutor.execute")
    def test_delete_health_monitor(self, mock_execute):
        hm = factories.HealthMonitorFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.delete(factories.HealthMonitorFactory.get_url(hm))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()


class HealthMonitorUpdateTest(BaseHealthMonitorTest):
    @mock.patch("waldur_openstack.executors.HealthMonitorUpdateExecutor.execute")
    def test_update_health_monitor_delay(self, mock_execute):
        hm = factories.HealthMonitorFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.patch(
            factories.HealthMonitorFactory.get_url(hm),
            {"delay": 15},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_execute.assert_called_once()


class BaseListenerTest(BaseLoadBalancerTest):
    def setUp(self) -> None:
        super().setUp()
        self.load_balancer = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="lb_backend_123",
            state=CoreStates.OK,
        )


class ListenerListTest(BaseListenerTest):
    def test_list_listeners(self):
        listener = factories.ListenerFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.get(factories.ListenerFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], listener.name)
        self.assertEqual(response.data[0]["protocol"], listener.protocol)
        self.assertEqual(response.data[0]["protocol_port"], listener.protocol_port)


class ListenerCreateTest(BaseListenerTest):
    @mock.patch("waldur_openstack.executors.ListenerCreateExecutor.execute")
    def test_create_listener(self, mock_execute):
        valid_data = {
            "name": "Test Listener",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
            "protocol_port": 80,
        }
        response = self.client.post(
            factories.ListenerFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_execute.assert_called_once()

    def test_create_fails_when_load_balancer_not_provisioned(self):
        self.load_balancer.backend_id = None
        self.load_balancer.save()
        valid_data = {
            "name": "Test Listener",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
            "protocol_port": 80,
        }
        response = self.client.post(
            factories.ListenerFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be provisioned", str(response.data))

    def test_create_listener_with_default_pool(self):
        pool = factories.PoolFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        valid_data = {
            "name": "Listener With Pool",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
            "protocol_port": 8080,
            "default_pool": factories.PoolFactory.get_url(pool),
        }
        response = self.client.post(
            factories.ListenerFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        listener = models.Listener.objects.get(name="Listener With Pool")
        self.assertEqual(listener.default_pool_id, pool.id)

    def test_create_listener_rejects_default_pool_from_other_load_balancer(self):
        other_lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="lb_backend_other",
            state=CoreStates.OK,
        )
        foreign_pool = factories.PoolFactory(
            load_balancer=other_lb,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        valid_data = {
            "name": "Bad Pool Ref",
            "load_balancer": factories.LoadBalancerFactory.get_url(self.load_balancer),
            "protocol": "TCP",
            "protocol_port": 80,
            "default_pool": factories.PoolFactory.get_url(foreign_pool),
        }
        response = self.client.post(
            factories.ListenerFactory.get_list_url(), valid_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("same load balancer", str(response.data))


class ListenerDeleteTest(BaseListenerTest):
    @mock.patch("waldur_openstack.executors.ListenerDeleteExecutor.execute")
    def test_delete_listener(self, mock_execute):
        listener = factories.ListenerFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.delete(factories.ListenerFactory.get_url(listener))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()


class ListenerUpdateTest(BaseListenerTest):
    @mock.patch("waldur_openstack.executors.ListenerUpdateExecutor.execute")
    def test_update_listener_name(self, mock_execute):
        listener = factories.ListenerFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self.client.patch(
            factories.ListenerFactory.get_url(listener),
            {"name": "Updated Listener Name"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_execute.assert_called_once()


class LoadBalancerPullTest(BaseLoadBalancerTest):
    def setUp(self):
        super().setUp()
        self.lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
        )
        self.url = factories.LoadBalancerFactory.get_url(self.lb, action="pull")

    @mock.patch("waldur_openstack.executors.LoadBalancerPullExecutor.execute")
    def test_pull_load_balancer_in_ok_state(self, mock_execute):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.LoadBalancerPullExecutor.execute")
    def test_pull_load_balancer_in_erred_state(self, mock_execute):
        self.lb.state = CoreStates.ERRED
        self.lb.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_pull_load_balancer_in_updating_state_is_rejected(self):
        self.lb.state = CoreStates.UPDATING
        self.lb.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_pull_load_balancer_in_creating_state_is_rejected(self):
        self.lb.state = CoreStates.CREATING
        self.lb.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class PoolPullTest(BasePoolTest):
    def setUp(self):
        super().setUp()
        self.pool = factories.PoolFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
        )
        self.url = factories.PoolFactory.get_url(self.pool, action="pull")

    @mock.patch("waldur_openstack.executors.PoolPullExecutor.execute")
    def test_pull_pool_in_ok_state(self, mock_execute):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.PoolPullExecutor.execute")
    def test_pull_pool_in_erred_state(self, mock_execute):
        self.pool.state = CoreStates.ERRED
        self.pool.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_pull_pool_in_updating_state_is_rejected(self):
        self.pool.state = CoreStates.UPDATING
        self.pool.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch("waldur_openstack.executors.PoolPullExecutor.execute")
    def test_pull_pool_passes_load_balancer_to_executor(self, mock_execute):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        _, kwargs = mock_execute.call_args
        self.assertIn("serialized_load_balancer", kwargs)


class ListenerPullTest(BaseListenerTest):
    def setUp(self):
        super().setUp()
        self.listener = factories.ListenerFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
        )
        self.url = factories.ListenerFactory.get_url(self.listener, action="pull")

    @mock.patch("waldur_openstack.executors.ListenerPullExecutor.execute")
    def test_pull_listener_in_ok_state(self, mock_execute):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.ListenerPullExecutor.execute")
    def test_pull_listener_in_erred_state(self, mock_execute):
        self.listener.state = CoreStates.ERRED
        self.listener.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_pull_listener_in_updating_state_is_rejected(self):
        self.listener.state = CoreStates.UPDATING
        self.listener.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch("waldur_openstack.executors.ListenerPullExecutor.execute")
    def test_pull_listener_passes_load_balancer_and_tenant_to_executor(
        self, mock_execute
    ):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        _, kwargs = mock_execute.call_args
        self.assertIn("serialized_load_balancer", kwargs)
        self.assertIn("serialized_tenant", kwargs)


class PoolMemberPullTest(BasePoolMemberTest):
    def setUp(self):
        super().setUp()
        self.member = factories.PoolMemberFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
        )
        self.url = factories.PoolMemberFactory.get_url(self.member, action="pull")

    @mock.patch("waldur_openstack.executors.PoolMemberPullExecutor.execute")
    def test_pull_pool_member_in_ok_state(self, mock_execute):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.PoolMemberPullExecutor.execute")
    def test_pull_pool_member_in_erred_state(self, mock_execute):
        self.member.state = CoreStates.ERRED
        self.member.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_pull_pool_member_in_updating_state_is_rejected(self):
        self.member.state = CoreStates.UPDATING
        self.member.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch("waldur_openstack.executors.PoolMemberPullExecutor.execute")
    def test_pull_pool_member_passes_pool_and_load_balancer_to_executor(
        self, mock_execute
    ):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        _, kwargs = mock_execute.call_args
        self.assertIn("serialized_pool", kwargs)
        self.assertIn("serialized_load_balancer", kwargs)


class HealthMonitorPullTest(BaseHealthMonitorTest):
    def setUp(self):
        super().setUp()
        self.hm = factories.HealthMonitorFactory(
            pool=self.pool,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
        )
        self.url = factories.HealthMonitorFactory.get_url(self.hm, action="pull")

    @mock.patch("waldur_openstack.executors.HealthMonitorPullExecutor.execute")
    def test_pull_health_monitor_in_ok_state(self, mock_execute):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.HealthMonitorPullExecutor.execute")
    def test_pull_health_monitor_in_erred_state(self, mock_execute):
        self.hm.state = CoreStates.ERRED
        self.hm.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_pull_health_monitor_in_updating_state_is_rejected(self):
        self.hm.state = CoreStates.UPDATING
        self.hm.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch("waldur_openstack.executors.HealthMonitorPullExecutor.execute")
    def test_pull_health_monitor_passes_pool_and_load_balancer_to_executor(
        self, mock_execute
    ):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        _, kwargs = mock_execute.call_args
        self.assertIn("serialized_pool", kwargs)
        self.assertIn("serialized_load_balancer", kwargs)
