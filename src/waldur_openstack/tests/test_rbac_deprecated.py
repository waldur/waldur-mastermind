from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_openstack import models

from . import factories, fixtures


@ddt
class CreateRbacPolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.neutron_client_patcher = mock.patch(
            "waldur_openstack.backend.get_neutron_client"
        )
        self.mock_neutron_client = self.neutron_client_patcher.start()
        self.mock_neutron_client().create_rbac_policy.return_value = {
            "rbac_policy": {"id": 1}
        }

        self.keystone_session_patcher = mock.patch(
            "waldur_openstack.backend.get_keystone_session"
        )
        self.mock_keystone_session = self.keystone_session_patcher.start()

        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.network = self.fixture.network
        self.target_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="other_backend_id"
        )
        self.url = f"/api/openstack-networks/{self.network.uuid}/rbac_policy_create/"

    def tearDown(self):
        self.neutron_client_patcher.stop()
        super().tearDown()

    @data("staff", "owner", "admin", "manager")
    def test_authorized_user_can_create_rbac_policy(self, user):
        self.target_tenant.project.add_user(self.fixture.owner, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.admin, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.manager, ProjectRole.MANAGER)

        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.NetworkRBACPolicy.objects.filter(
                network=self.network,
                target_tenant=self.target_tenant,
                policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
            ).exists()
        )

    @data("owner", "admin", "manager")
    def test_user_without_target_tenant_permissions_cannot_create_rbac_policy(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_cannot_create_rbac_policy(self):
        self.client.force_authenticate(self.fixture.member)
        payload = {
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class DeleteRbacPolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.neutron_client_patcher = mock.patch(
            "waldur_openstack.backend.get_neutron_client"
        )
        self.mock_neutron_client = self.neutron_client_patcher.start()

        self.keystone_session_patcher = mock.patch(
            "waldur_openstack.backend.get_keystone_session"
        )
        self.mock_keystone_session = self.keystone_session_patcher.start()

        self.fixture = fixtures.OpenStackFixture()
        self.network = self.fixture.network
        self.rbac_policy = factories.NetworkRBACPolicyFactory(network=self.network)
        self.target_tenant = self.rbac_policy.target_tenant
        self.url = f"/api/openstack-networks/{self.network.uuid}/rbac_policy_delete/{self.rbac_policy.uuid}/"

    def tearDown(self):
        self.neutron_client_patcher.stop()
        super().tearDown()

    @data("staff", "owner", "admin", "manager")
    def test_authorized_user_can_delete_rbac_policy(self, user):
        self.target_tenant.project.add_user(self.fixture.owner, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.admin, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.manager, ProjectRole.MANAGER)

        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(uuid=self.rbac_policy.uuid).exists()
        )

    @data("owner", "admin", "manager")
    def test_user_without_target_tenant_permissions_cannot_delete_rbac_policy(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_cannot_delete_rbac_policy(self):
        self.client.force_authenticate(getattr(self.fixture, "member"))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
