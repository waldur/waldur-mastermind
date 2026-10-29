"""Contract test: a network shared by a project Waldur does not manage, against a
REAL OpenStack (the emulator will do).

Opt-in, and skipped unless the same variables the other live contract tests use
are set::

    export WALDUR_LIVE_OS_AUTH_URL=http://localhost:5000/v3
    export WALDUR_LIVE_OS_USERNAME=admin
    export WALDUR_LIVE_OS_PASSWORD=...
    export WALDUR_LIVE_OS_PROJECT_NAME=admin
    export WALDUR_LIVE_OS_PROJECT_ID=<that project's id>

The admin project owns the network and has no tenant in Waldur, which is the
situation the unit tests can only describe: whether the policy listing, the
network lookup and the subnet listing on the admin session really carry the
share to the consumer is the cloud's behaviour, not Waldur's.

The tests run twice: once signed in as the cloud's admin, and once as a user of
a domain the test creates, with the tenants in that domain -- as Waldur usually
runs, and the case where the admin project is not among the cloud's projects.
"""

import os
import unittest
import uuid
from unittest import mock

from django.test import TransactionTestCase
from keystoneauth1 import session as keystone_session
from keystoneauth1.identity import v3 as keystone_identity
from keystoneclient import exceptions as keystone_exceptions
from keystoneclient.v3 import client as keystone_client
from neutronclient.common import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.core.utils import serialize_instance
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.utils import handle_resource_not_found
from waldur_openstack import executors, models, tasks
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.session import get_keystone_client

from . import factories

LIVE_ENV = (
    "WALDUR_LIVE_OS_AUTH_URL",
    "WALDUR_LIVE_OS_USERNAME",
    "WALDUR_LIVE_OS_PASSWORD",
    "WALDUR_LIVE_OS_PROJECT_NAME",
    "WALDUR_LIVE_OS_PROJECT_ID",
)

CIDR = "10.97.0.0/24"
SECOND_CIDR = "10.97.1.0/24"


def live_config():
    return {name: os.environ.get(name) for name in LIVE_ENV}


def tenant_pull_steps(tenant):
    """Backend method names of the periodic tenant pull, in order."""
    chain = executors.ExistingTenantPullExecutor.get_task_signature(
        tenant, serialize_instance(tenant)
    )
    return [sig.kwargs.get("backend_method") or sig.args[1] for sig in chain.tasks]


@unittest.skipUnless(
    all(os.environ.get(name) for name in LIVE_ENV),
    "live OpenStack credentials not configured; set %s" % ", ".join(LIVE_ENV),
)
class LiveUnmanagedOwnerShareTest(TransactionTestCase):
    def setUp(self):
        super().setUp()
        cfg = live_config()
        self.admin_project_id = cfg["WALDUR_LIVE_OS_PROJECT_ID"]
        self.suffix = uuid.uuid4().hex[:8]
        credentials = self.cloud_credentials(cfg)
        self.settings = factories.SettingsFactory(
            backend_url=cfg["WALDUR_LIVE_OS_AUTH_URL"],
            username=credentials["username"],
            password=credentials["password"],
            domain=credentials["domain"],
            options={"tenant_name": credentials["project_name"]},
            customer=structure_factories.CustomerFactory(),
            shared=True,
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(self.settings)
        self.neutron = neutron_client.Client(session=self.backend.admin_session)

        self.backend_network = self.neutron.create_network(
            {
                "network": {
                    "name": f"provider-lan-{self.suffix}",
                    "tenant_id": self.admin_project_id,
                }
            }
        )["network"]
        self.addCleanup(
            self.drop, self.neutron.delete_network, self.backend_network["id"]
        )
        self.backend_subnet = self.neutron.create_subnet(
            {
                "subnet": {
                    "name": f"provider-lan-subnet-{self.suffix}",
                    "network_id": self.backend_network["id"],
                    "tenant_id": self.admin_project_id,
                    "ip_version": 4,
                    "cidr": CIDR,
                }
            }
        )["subnet"]
        self.addCleanup(
            self.drop, self.neutron.delete_subnet, self.backend_subnet["id"]
        )

        self.consumer_project = structure_factories.ProjectFactory()
        self.credentials = credentials
        self.consumer = self.create_tenant(
            f"consumer-{self.suffix}", self.consumer_project
        )

        self.rbac = self.neutron.create_rbac_policy(
            {
                "rbac_policy": {
                    "object_type": "network",
                    "object_id": self.backend_network["id"],
                    "action": "access_as_shared",
                    "target_tenant": self.consumer.backend_id,
                }
            }
        )["rbac_policy"]
        self.addCleanup(self.drop, self.neutron.delete_rbac_policy, self.rbac["id"])

    def create_tenant(self, name, project):
        tenant = factories.TenantFactory(
            name=name,
            service_settings=self.settings,
            project=project,
            backend_id="",
            state=CoreStates.OK,
            user_username=self.credentials["username"],
            user_password=self.credentials["password"],
        )
        self.backend.create_tenant(tenant)
        tenant.refresh_from_db()
        # Provisioning grants the settings' admin a role in every tenant it
        # creates; without it the tenant-scoped steps of the pull get a 401.
        self.backend.add_admin_user_to_tenant(tenant)
        self.addCleanup(self.drop_project, tenant.backend_id)
        return tenant

    def add_subnet(self):
        """A second subnet on the network, created in Neutron behind Waldur."""
        subnet = self.neutron.create_subnet(
            {
                "subnet": {
                    "name": f"provider-lan-subnet-2-{self.suffix}",
                    "network_id": self.backend_network["id"],
                    "tenant_id": self.admin_project_id,
                    "ip_version": 4,
                    "cidr": SECOND_CIDR,
                }
            }
        )["subnet"]
        self.addCleanup(self.drop, self.neutron.delete_subnet, subnet["id"])
        return subnet

    def cloud_credentials(self, cfg):
        """What Waldur signs in to the cloud with: here, the cloud's admin."""
        return {
            "username": cfg["WALDUR_LIVE_OS_USERNAME"],
            "password": cfg["WALDUR_LIVE_OS_PASSWORD"],
            "project_name": cfg["WALDUR_LIVE_OS_PROJECT_NAME"],
            "domain": "",
        }

    def drop(self, delete, backend_id):
        try:
            delete(backend_id)
        except neutron_exceptions.NeutronClientException:
            pass

    def drop_project(self, backend_id):
        # Neutron creates a default security group in every project and keeps
        # it after the project is gone; on a real cloud that is a leak.
        for group in self.neutron.list_security_groups(project_id=backend_id)[
            "security_groups"
        ]:
            self.drop(self.neutron.delete_security_group, group["id"])
        try:
            get_keystone_client(self.backend.admin_session).projects.delete(backend_id)
        except keystone_exceptions.ClientException:
            pass

    def pull(self, tenant):
        """Every step of the periodic tenant pull, as the worker runs it."""
        for step in tenant_pull_steps(tenant):
            getattr(self.backend, step)(tenant)

    def pull_consumer(self):
        self.pull(self.consumer)

    def owner(self):
        return models.Tenant.objects.get(
            service_settings=self.settings, backend_id=self.admin_project_id
        )

    def consumer_client(self):
        return self.project_admin_client(self.consumer_project)

    def project_admin_client(self, project):
        user = structure_factories.UserFactory()
        project.add_user(user, ProjectRole.ADMIN)
        client = test.APIClient()
        client.force_authenticate(user)
        return client

    def consumer_lists(self, factory):
        response = self.consumer_client().get(
            factory.get_list_url(), {"tenant_uuid": self.consumer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["backend_id"] for item in response.data]

    def test_the_consumer_sees_the_share(self):
        self.pull_consumer()

        owner = models.Tenant.objects.get(
            service_settings=self.settings, backend_id=self.admin_project_id
        )
        self.assertFalse(owner.is_managed)
        self.assertEqual(owner.state, CoreStates.OK)
        network = models.Network.objects.get(backend_id=self.backend_network["id"])
        self.assertEqual(network.tenant, owner)
        self.assertIn(
            self.backend_network["id"], self.consumer_lists(factories.NetworkFactory)
        )
        self.assertIn(
            self.backend_subnet["id"], self.consumer_lists(factories.SubNetFactory)
        )

    def test_revoking_the_share_forgets_it_and_leaves_the_cloud_alone(self):
        self.pull_consumer()

        self.neutron.delete_rbac_policy(self.rbac["id"])
        self.pull_consumer()

        self.assertFalse(
            models.Network.objects.filter(
                backend_id=self.backend_network["id"]
            ).exists()
        )
        self.assertFalse(
            models.Tenant.objects.filter(
                service_settings=self.settings, is_managed=False
            ).exists()
        )
        # Only the local rows go: the network is still the admin project's.
        self.assertEqual(
            self.neutron.show_network(self.backend_network["id"])["network"]["id"],
            self.backend_network["id"],
        )

    def test_the_cloud_refresh_brings_what_the_tenant_pull_leaves(self):
        self.pull_consumer()
        renamed = f"provider-lan-renamed-{self.suffix}"
        self.neutron.update_network(
            self.backend_network["id"], {"network": {"name": renamed}}
        )
        added = self.add_subnet()

        self.pull_consumer()

        network = models.Network.objects.get(backend_id=self.backend_network["id"])
        self.assertNotEqual(network.name, renamed)
        self.assertNotIn(added["id"], self.consumer_lists(factories.SubNetFactory))

        tasks.pull_networks_of_unmanaged_owners()

        network.refresh_from_db()
        self.assertEqual(network.name, renamed)
        self.assertIn(added["id"], self.consumer_lists(factories.SubNetFactory))

    @mock.patch.object(executors.PortCreateExecutor, "execute")
    def test_a_tenant_left_out_of_the_share_gets_none_of_it(self, execute):
        outsider_project = structure_factories.ProjectFactory()
        outsider = self.create_tenant(f"outsider-{self.suffix}", outsider_project)
        self.pull_consumer()
        self.pull(outsider)
        added = self.add_subnet()
        tasks.pull_networks_of_unmanaged_owners()
        self.assertIn(added["id"], self.consumer_lists(factories.SubNetFactory))

        client = self.project_admin_client(outsider_project)
        shared = {
            factories.NetworkFactory: {self.backend_network["id"]},
            factories.SubNetFactory: {self.backend_subnet["id"], added["id"]},
        }
        for factory, backend_ids in shared.items():
            for params in ({}, {"tenant_uuid": outsider.uuid.hex}):
                with self.subTest(factory=factory.__name__, params=params):
                    response = client.get(factory.get_list_url(), params)
                    self.assertEqual(response.status_code, status.HTTP_200_OK)
                    listed = {item["backend_id"] for item in response.data}
                    self.assertFalse(listed & backend_ids)

        network = models.Network.objects.get(backend_id=self.backend_network["id"])
        response = client.post(
            factories.PortFactory.get_list_url(),
            {
                "name": f"port-{self.suffix}",
                "network": factories.NetworkFactory.get_url(network),
                "target_tenant": factories.TenantFactory.get_url(outsider),
                "fixed_ips": [{"subnet_id": self.backend_subnet["id"]}],
                "port_security_enabled": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("target_tenant", response.data)
        execute.assert_not_called()
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(target_tenant=outsider).exists()
        )

    @mock.patch.object(executors.PortCreateExecutor, "execute")
    def test_the_consumer_creates_a_port_on_it_with_port_security_on(self, execute):
        self.pull_consumer()
        network = models.Network.objects.get(backend_id=self.backend_network["id"])
        client = self.consumer_client()
        # What the Create port dialog sends for a network shared with the tenant.
        port = {
            "name": f"port-{self.suffix}",
            "network": factories.NetworkFactory.get_url(network),
            "target_tenant": factories.TenantFactory.get_url(self.consumer),
            "fixed_ips": [{"subnet_id": self.backend_subnet["id"]}],
        }

        refused = client.post(
            factories.PortFactory.get_list_url(),
            {**port, "port_security_enabled": False},
            format="json",
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)

        response = client.post(
            factories.PortFactory.get_list_url(),
            {**port, "port_security_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        execute.assert_called_once()

        created = models.Port.objects.get(uuid=response.data["uuid"])
        self.backend.create_port(created)
        self.addCleanup(self.drop, self.neutron.delete_port, created.backend_id)

        backend_port = self.neutron.show_port(created.backend_id)["port"]
        self.assertEqual(backend_port["project_id"], self.consumer.backend_id)
        self.assertEqual(backend_port["network_id"], self.backend_network["id"])
        self.assertTrue(backend_port["port_security_enabled"])
        self.assertEqual(
            [fixed_ip["subnet_id"] for fixed_ip in backend_port["fixed_ips"]],
            [self.backend_subnet["id"]],
        )


class LiveOwnerOutsideTheDomainTest(LiveUnmanagedOwnerShareTest):
    """Waldur signs in to a domain of its own and creates its tenants there.

    The admin project that shares the network stays in its own domain, so the
    cloud's listing of its projects never shows the network's owner.
    """

    def cloud_credentials(self, cfg):
        admin_session = keystone_session.Session(
            auth=keystone_identity.Password(
                auth_url=cfg["WALDUR_LIVE_OS_AUTH_URL"],
                username=cfg["WALDUR_LIVE_OS_USERNAME"],
                password=cfg["WALDUR_LIVE_OS_PASSWORD"],
                project_name=cfg["WALDUR_LIVE_OS_PROJECT_NAME"],
                user_domain_name="Default",
                project_domain_name="Default",
            )
        )
        keystone = keystone_client.Client(session=admin_session)
        neutron = neutron_client.Client(session=admin_session)

        domain = keystone.domains.create(name=f"waldur-live-{self.suffix}")
        self.addCleanup(self.drop_domain, keystone, domain)
        project = keystone.projects.create(name=f"waldur-{self.suffix}", domain=domain)
        self.addCleanup(self.drop_own_project, keystone, neutron, project)
        password = uuid.uuid4().hex
        user = keystone.users.create(
            name=f"waldur-{self.suffix}",
            domain=domain,
            password=password,
            default_project=project,
        )
        self.addCleanup(keystone.users.delete, user)
        keystone.roles.grant(
            keystone.roles.find(name="admin"), user=user, project=project
        )
        return {
            "username": user.name,
            "password": password,
            "project_name": project.name,
            "domain": domain.name,
        }

    def drop_own_project(self, keystone, neutron, project):
        # Waldur's calls on its project's session give it a default group.
        for group in neutron.list_security_groups(project_id=project.id)[
            "security_groups"
        ]:
            self.drop(neutron.delete_security_group, group["id"])
        try:
            keystone.projects.delete(project)
        except keystone_exceptions.ClientException:
            pass

    def drop_domain(self, keystone, domain):
        try:
            keystone.domains.update(domain, enabled=False)
            keystone.domains.delete(domain)
        except keystone_exceptions.ClientException:
            pass

    def test_the_cloud_pull_keeps_the_owner(self):
        self.pull_consumer()

        self.backend.pull_tenants()

        owner = self.owner()
        self.assertEqual(owner.state, CoreStates.OK)
        self.assertEqual(owner.error_message, "")
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.state, CoreStates.OK)

    def test_an_owner_marked_missing_recovers(self):
        self.pull_consumer()
        # What the cloud's pull did to it before it left unmanaged owners alone.
        handle_resource_not_found(self.owner())
        self.assertEqual(self.owner().state, CoreStates.ERRED)

        tasks.pull_networks_of_unmanaged_owners()

        owner = self.owner()
        self.assertEqual(owner.state, CoreStates.OK)
        self.assertEqual(owner.error_message, "")
