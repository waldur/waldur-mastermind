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
"""

import os
import unittest
import uuid

from django.test import TransactionTestCase
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.common import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.core.utils import serialize_instance
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import executors, models
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
        self.settings = factories.SettingsFactory(
            backend_url=cfg["WALDUR_LIVE_OS_AUTH_URL"],
            username=cfg["WALDUR_LIVE_OS_USERNAME"],
            password=cfg["WALDUR_LIVE_OS_PASSWORD"],
            options={"tenant_name": cfg["WALDUR_LIVE_OS_PROJECT_NAME"]},
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
        self.consumer = factories.TenantFactory(
            name=f"consumer-{self.suffix}",
            service_settings=self.settings,
            project=self.consumer_project,
            backend_id="",
            state=CoreStates.OK,
            user_username=cfg["WALDUR_LIVE_OS_USERNAME"],
            user_password=cfg["WALDUR_LIVE_OS_PASSWORD"],
        )
        self.backend.create_tenant(self.consumer)
        self.consumer.refresh_from_db()
        # Provisioning grants the settings' admin a role in every tenant it
        # creates; without it the tenant-scoped steps of the pull get a 401.
        self.backend.add_admin_user_to_tenant(self.consumer)
        self.addCleanup(self.drop_project, self.consumer.backend_id)

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

    def pull_consumer(self):
        """Every step of the periodic tenant pull, as the worker runs it."""
        for step in tenant_pull_steps(self.consumer):
            getattr(self.backend, step)(self.consumer)

    def consumer_lists(self, factory):
        user = structure_factories.UserFactory()
        self.consumer_project.add_user(user, ProjectRole.ADMIN)
        client = test.APIClient()
        client.force_authenticate(user)
        response = client.get(
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
