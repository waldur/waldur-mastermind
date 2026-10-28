"""Contract test: a provider network shared with every project (`*`) reaches a
tenant Waldur creates, and a VM on its IPv6 subnet is reachable, against a REAL
OpenStack.

Opt-in. Besides the variables the other live contract tests use::

    export WALDUR_LIVE_OS_AUTH_URL=https://keystone.example.org/v3
    export WALDUR_LIVE_OS_USERNAME=admin
    export WALDUR_LIVE_OS_PASSWORD=...
    export WALDUR_LIVE_OS_PROJECT_NAME=admin
    export WALDUR_LIVE_OS_PROJECT_ID=<that project's id>

it needs an image to boot and the machine running it to have IPv6::

    export WALDUR_LIVE_OS_DOMAIN=Default            # of the user, its project and new tenants
    export WALDUR_LIVE_OS_IMAGE="Debian 13 (Trixie)"
    export WALDUR_LIVE_OS_IMAGE_USER=debian
    export WALDUR_LIVE_OS_FLAVOR=m1.small
    export WALDUR_LIVE_OS_VOLUME_TYPE=<optional>

The cloud must have an external network shared with `*` that has an IPv6
subnet: the usual way an operator publishes a provider network. The test
creates a tenant the way an order does, boots a VM with a port on that subnet,
checks it from here over IPv6, and deletes everything it created -- it never
creates or removes an RBAC policy.

Celery does not run here: each executor's chain is run step by step, and the
polls wait in a sleep loop instead of the task's retries.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest import mock

from cinderclient import exceptions as cinder_exceptions
from django.test import TransactionTestCase
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.common import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from novaclient import exceptions as nova_exceptions
from rest_framework import status

from waldur_core.core import tasks as core_tasks
from waldur_core.core.enums import CoreStates
from waldur_core.core.utils import deserialize_instance, serialize_instance
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace_openstack import views as marketplace_views
from waldur_openstack import executors, models, views
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.session import (
    get_cinder_client,
    get_keystone_client,
    get_nova_client,
)

from . import factories

LIVE_ENV = (
    "WALDUR_LIVE_OS_AUTH_URL",
    "WALDUR_LIVE_OS_USERNAME",
    "WALDUR_LIVE_OS_PASSWORD",
    "WALDUR_LIVE_OS_PROJECT_NAME",
    "WALDUR_LIVE_OS_PROJECT_ID",
)
VM_ENV = (
    "WALDUR_LIVE_OS_IMAGE",
    "WALDUR_LIVE_OS_IMAGE_USER",
    "WALDUR_LIVE_OS_FLAVOR",
)

POLL_SECONDS = 5
POLL_TIMEOUT = 600
# A boot from a volume and cloud-init's first run.
REACHABLE_TIMEOUT = 600
SYSTEM_VOLUME_MIB = 5 * 1024


def live_config():
    return {name: os.environ.get(name) for name in LIVE_ENV + VM_ENV}


def ping6_command(address):
    if shutil.which("ping6"):
        return ["ping6", "-c", "1", address]
    return ["ping", "-6", "-c", "1", "-W", "2", address]


@unittest.skipUnless(
    all(os.environ.get(name) for name in LIVE_ENV + VM_ENV),
    "live OpenStack credentials not configured; set %s" % ", ".join(LIVE_ENV + VM_ENV),
)
class LiveWildcardSharedNetworkTest(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = live_config()
        self.suffix = uuid.uuid4().hex[:8]
        self.settings = factories.SettingsFactory(
            backend_url=self.cfg["WALDUR_LIVE_OS_AUTH_URL"],
            username=self.cfg["WALDUR_LIVE_OS_USERNAME"],
            password=self.cfg["WALDUR_LIVE_OS_PASSWORD"],
            domain=os.environ.get("WALDUR_LIVE_OS_DOMAIN") or "Default",
            options={"tenant_name": self.cfg["WALDUR_LIVE_OS_PROJECT_NAME"]},
            customer=structure_factories.CustomerFactory(),
            shared=True,
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(self.settings)
        self.neutron = neutron_client.Client(session=self.backend.admin_session)

        self.provider_network, self.provider_subnet = self.find_provider_network()
        self.settings.options["external_network_id"] = self.provider_network["id"]
        self.settings.save()
        # Service settings sync does this before any tenant is ordered.
        self.backend.pull_external_networks()

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.project = structure_factories.ProjectFactory(
            customer=self.settings.customer
        )
        self.key_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.key_dir, ignore_errors=True)

    def find_provider_network(self):
        wildcard_networks = {
            p["object_id"]
            for p in self.neutron.list_rbac_policies(
                object_type="network",
                action="access_as_shared",
                target_tenant="*",
            )["rbac_policies"]
        }
        for network in self.neutron.list_networks(**{"router:external": True})[
            "networks"
        ]:
            if network["id"] not in wildcard_networks:
                continue
            for subnet in self.neutron.list_subnets(network_id=network["id"])[
                "subnets"
            ]:
                if subnet["ip_version"] == 6:
                    return network, subnet
        self.skipTest("no external network shared with * has an IPv6 subnet")

    # Running executors without Celery

    def run_executor(self, executor, instance, **kwargs):
        """What `executor.execute` queues, run in this process."""
        executor.pre_apply(instance, is_async=False, **kwargs)
        serialized = serialize_instance(instance)
        self.run_signature(executor.get_task_signature(instance, serialized, **kwargs))
        success = executor.get_success_signature(instance, serialized, **kwargs)
        if success:
            success.apply(throw=True)

    def run_signature(self, signature):
        if hasattr(signature, "tasks"):
            for task in signature.tasks:
                self.run_signature(task)
            return
        task = signature.type
        if isinstance(task, core_tasks.PollRuntimeStateTask):
            self.poll_runtime_state(signature)
        elif isinstance(task, core_tasks.PollBackendCheckTask):
            self.poll_backend_check(signature)
        else:
            time.sleep(signature.options.get("countdown") or 0)
            signature.apply(throw=True)

    def poll_runtime_state(self, signature):
        instance = deserialize_instance(signature.args[0])
        method = signature.kwargs["backend_pull_method"]
        success_state = signature.kwargs["success_state"]
        erred_state = signature.kwargs["erred_state"]
        deadline = time.monotonic() + POLL_TIMEOUT
        while True:
            getattr(instance.get_backend(), method)(instance)
            instance.refresh_from_db()
            if instance.runtime_state == success_state:
                return
            self.assertNotEqual(
                instance.runtime_state, erred_state, f"{instance} erred"
            )
            self.assertLess(time.monotonic(), deadline, f"{instance} stuck in {method}")
            time.sleep(POLL_SECONDS)

    def poll_backend_check(self, signature):
        instance = deserialize_instance(signature.args[0])
        method = signature.kwargs.get("backend_check_method") or signature.args[1]
        deadline = time.monotonic() + POLL_TIMEOUT
        while not getattr(instance.get_backend(), method)(instance):
            self.assertLess(time.monotonic(), deadline, f"{instance} stuck in {method}")
            time.sleep(POLL_SECONDS)

    # Orders, as the marketplace processors send them

    def order_tenant(self):
        view = marketplace_views.MarketplaceTenantViewSet.as_view({"post": "create"})
        with mock.patch.object(marketplace_views, "TenantCreateExecutor"):
            response = common_utils.create_request(
                view,
                self.staff,
                {
                    "service_settings": factories.SettingsFactory.get_url(
                        self.settings
                    ),
                    "project": structure_factories.ProjectFactory.get_url(self.project),
                    "name": f"wal-live-{self.suffix}",
                },
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        tenant = models.Tenant.objects.get(uuid=response.data["uuid"])
        self.addCleanup(self.drop_tenant, tenant.pk)
        return tenant

    def order_instance(self, tenant, subnets, ssh_key):
        cfg = self.cfg
        image = models.Image.objects.get(
            settings=self.settings, name=cfg["WALDUR_LIVE_OS_IMAGE"]
        )
        flavor = models.Flavor.objects.get(
            settings=self.settings, name=cfg["WALDUR_LIVE_OS_FLAVOR"]
        )
        payload = {
            "tenant": factories.TenantFactory.get_url(tenant),
            "project": structure_factories.ProjectFactory.get_url(self.project),
            "name": f"wal-live-{self.suffix}",
            "flavor": factories.FlavorFactory.get_url(flavor),
            "image": factories.ImageFactory.get_url(image),
            "system_volume_size": max(image.min_disk, SYSTEM_VOLUME_MIB),
            "ports": [
                {"subnet": factories.SubNetFactory.get_url(subnet)}
                for subnet in subnets
            ],
            # The "ssh" and "ping" groups only let traffic in; "default" is
            # what lets the guest out -- to DHCP and the metadata service.
            "security_groups": [
                {"url": factories.SecurityGroupFactory.get_url(group)}
                for group in tenant.security_groups.filter(
                    name__in=["default", "ssh", "ping"]
                )
            ],
            "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(ssh_key),
        }
        self.assertEqual(len(payload["security_groups"]), 3)
        volume_type = os.environ.get("WALDUR_LIVE_OS_VOLUME_TYPE")
        if volume_type:
            payload["system_volume_type"] = factories.VolumeTypeFactory.get_url(
                models.VolumeType.objects.get(settings=self.settings, name=volume_type)
            )
        view = views.MarketplaceInstanceViewSet.as_view({"post": "create"})
        with mock.patch.object(executors.InstanceCreateExecutor, "execute"):
            response = common_utils.create_request(view, self.staff, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return models.Instance.objects.get(uuid=response.data["uuid"]), flavor

    def make_ssh_key(self):
        path = os.path.join(self.key_dir, "id_ed25519")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path], check=True
        )
        with open(path + ".pub") as public_key:
            key = structure_factories.SshPublicKeyFactory(
                user=self.staff, public_key=public_key.read().strip()
            )
        return key, path

    # Checks from this machine

    def wait_for_ping(self, address):
        deadline = time.monotonic() + REACHABLE_TIMEOUT
        while time.monotonic() < deadline:
            if (
                subprocess.run(
                    ping6_command(address), capture_output=True, timeout=30
                ).returncode
                == 0
            ):
                return True
            time.sleep(POLL_SECONDS)
        return False

    def ssh(self, address, key_path, command):
        deadline = time.monotonic() + REACHABLE_TIMEOUT
        result = None
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "ssh",
                    "-6",
                    "-i",
                    key_path,
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "BatchMode=yes",
                    f"{self.cfg['WALDUR_LIVE_OS_IMAGE_USER']}@{address}",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return result.stdout
            time.sleep(POLL_SECONDS)
        print(f"ssh to {address} failed: {result and result.stderr}")
        return None

    def console_log(self, instance):
        try:
            return get_nova_client(
                self.backend.admin_session
            ).servers.get_console_output(instance.backend_id, length=60)
        except nova_exceptions.ClientException as e:
            return f"console log unavailable: {e}"

    # The test

    def test_vm_on_the_provider_ipv6_subnet_of_a_new_tenant(self):
        policies_before = {
            p["id"] for p in self.neutron.list_rbac_policies()["rbac_policies"]
        }

        tenant = self.order_tenant()
        self.run_executor(executors.TenantCreateExecutor, tenant)
        tenant.refresh_from_db()
        self.assertEqual(tenant.state, CoreStates.OK, tenant.error_message)
        print(f"\ntenant {tenant.name} created as project {tenant.backend_id}")

        # Usable at once: no RBAC policy written, no pull run after creation.
        provider_network = models.Network.objects.get(
            backend_id=self.provider_network["id"]
        )
        self.assertFalse(provider_network.tenant.is_managed)
        self.assertTrue(
            models.NetworkRBACPolicy.objects.filter(
                network=provider_network, target_tenant=tenant
            ).exists()
        )
        provider_subnet = models.SubNet.objects.get(
            backend_id=self.provider_subnet["id"]
        )
        self.assertIn(provider_subnet, tenant.available_subnets)
        policies_after = self.neutron.list_rbac_policies()["rbac_policies"]
        self.assertFalse(
            [p for p in policies_after if p["target_tenant"] == tenant.backend_id]
        )
        self.assertLessEqual(policies_before, {p["id"] for p in policies_after})

        ssh_key, key_path = self.make_ssh_key()
        own_subnet = models.SubNet.objects.get(tenant=tenant)
        instance, flavor = self.order_instance(
            tenant, [own_subnet, provider_subnet], ssh_key
        )
        self.run_executor(
            executors.InstanceCreateExecutor, instance, ssh_key=ssh_key, flavor=flavor
        )
        instance.refresh_from_db()
        self.assertEqual(instance.state, CoreStates.OK, instance.error_message)

        port = instance.ports.get(subnet=provider_subnet)
        backend_port = self.neutron.show_port(port.backend_id)["port"]
        self.assertEqual(backend_port["network_id"], self.provider_network["id"])
        self.assertEqual(backend_port["project_id"], tenant.backend_id)
        self.assertEqual(
            {ip["subnet_id"] for ip in backend_port["fixed_ips"]},
            {self.provider_subnet["id"]},
            "the port must not take an address from another subnet",
        )
        self.assertEqual(
            set(backend_port["security_groups"]),
            set(instance.security_groups.values_list("backend_id", flat=True)),
        )
        address = backend_port["fixed_ips"][0]["ip_address"]
        print(
            f"VM {instance.backend_id} port {port.backend_id}: {address} "
            f"(binding {backend_port.get('binding:vif_type')})"
        )
        self.assertIn(address, instance.internal_ips)

        reachable = self.wait_for_ping(address)
        if not reachable:
            print(self.console_log(instance))
        self.assertTrue(reachable, f"{address} does not answer ping6")
        guest = self.ssh(address, key_path, "ip addr show; ip -6 route show")
        if guest is None:
            print(self.console_log(instance))
            self.fail(f"cannot log in to {address}")
        print(guest)
        self.assertIn(address, guest)
        # "default via ...", or "default nhid N via ..." on newer kernels.
        self.assertRegex(guest, r"(?m)^default .*via ")

        self.run_executor(
            executors.InstanceDeleteExecutor,
            instance,
            delete_volumes=True,
            release_floating_ips=True,
        )
        with self.assertRaises(neutron_exceptions.NotFound):
            self.neutron.show_port(port.backend_id)

        tenant.refresh_from_db()
        project_id = tenant.backend_id
        self.run_executor(executors.TenantDeleteExecutor, tenant)
        leftovers = self.leftovers(project_id)
        # Neutron makes a "default" group for every project and keeps it after
        # the project is gone; the cleanup removes it.
        groups = leftovers.pop("security_groups", [])
        other_groups = [group for group in groups if group["name"] != "default"]
        if other_groups:
            leftovers["security_groups"] = other_groups
        print(
            f"left after tenant deletion: {leftovers or 'nothing'}; "
            f"default groups: {len(groups) - len(other_groups)}"
        )
        self.assertEqual(leftovers, {})

    # Cleanup, scoped to the project this test created

    def project_security_groups(self, project_id):
        """The groups of a project, without making Neutron create one.

        Listing groups filtered by a project makes Neutron create that
        project's "default" group, even for a project that no longer exists,
        so the listing is unfiltered and the project picked out here.
        """
        return [
            group
            for group in self.neutron.list_security_groups()["security_groups"]
            if group["project_id"] == project_id
        ]

    def leftovers(self, project_id):
        nova = get_nova_client(self.backend.admin_session)
        cinder = get_cinder_client(self.backend.admin_session)
        found = {
            "servers": nova.servers.list(
                search_opts={"all_tenants": 1, "project_id": project_id}
            ),
            "volumes": cinder.volumes.list(
                search_opts={"all_tenants": 1, "project_id": project_id}
            ),
        }
        for kind in ("ports", "routers", "subnets", "networks", "floatingips"):
            found[kind] = getattr(self.neutron, f"list_{kind}")(project_id=project_id)[
                kind
            ]
        found["security_groups"] = self.project_security_groups(project_id)
        return {kind: items for kind, items in found.items() if items}

    def drop_tenant(self, tenant_pk):
        tenant = models.Tenant.objects.filter(pk=tenant_pk).first()
        if not tenant or not tenant.backend_id:
            return
        project_id = tenant.backend_id
        nova = get_nova_client(self.backend.admin_session)
        cinder = get_cinder_client(self.backend.admin_session)
        keystone = get_keystone_client(self.backend.admin_session)

        for server in nova.servers.list(
            search_opts={"all_tenants": 1, "project_id": project_id}
        ):
            self.quietly(server.delete)
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline and nova.servers.list(
            search_opts={"all_tenants": 1, "project_id": project_id}
        ):
            time.sleep(POLL_SECONDS)
        for volume in cinder.volumes.list(
            search_opts={"all_tenants": 1, "project_id": project_id}
        ):
            self.quietly(volume.delete)
        for ip in self.neutron.list_floatingips(project_id=project_id)["floatingips"]:
            self.quietly(self.neutron.delete_floatingip, ip["id"])
        for router in self.neutron.list_routers(project_id=project_id)["routers"]:
            for port in self.neutron.list_ports(device_id=router["id"])["ports"]:
                if port["device_owner"] != "network:router_gateway":
                    self.quietly(
                        self.neutron.remove_interface_router,
                        router["id"],
                        {"port_id": port["id"]},
                    )
            self.quietly(self.neutron.remove_gateway_router, router["id"])
            self.quietly(self.neutron.delete_router, router["id"])
        for port in self.neutron.list_ports(project_id=project_id)["ports"]:
            self.quietly(self.neutron.delete_port, port["id"])
        for network in self.neutron.list_networks(project_id=project_id)["networks"]:
            self.quietly(self.neutron.delete_network, network["id"])
        for group in self.project_security_groups(project_id):
            self.quietly(self.neutron.delete_security_group, group["id"])
        if tenant.user_username:
            for user in keystone.users.list(name=tenant.user_username):
                self.quietly(keystone.users.delete, user.id)
        self.quietly(keystone.projects.delete, project_id)

    def quietly(self, call, *args):
        try:
            call(*args)
        except (
            cinder_exceptions.ClientException,
            keystone_exceptions.ClientException,
            neutron_exceptions.NeutronClientException,
            nova_exceptions.ClientException,
        ):
            pass
