"""Contract test: servers and volumes created outside Waldur reach Waldur through
the background tenant resources pull, against a REAL OpenStack (the emulator
will do).

Opt-in, and skipped unless the same variables the other live contract tests use
are set::

    export WALDUR_LIVE_OS_AUTH_URL=http://localhost:5000/v3
    export WALDUR_LIVE_OS_USERNAME=admin
    export WALDUR_LIVE_OS_PASSWORD=...
    export WALDUR_LIVE_OS_PROJECT_NAME=admin
    export WALDUR_LIVE_OS_PROJECT_ID=<that project's id>

Each test creates a project for its tenant and deletes it afterwards. The
credentials must be admin ones, as Waldur's service settings are: the import
lists a tenant's servers with an ``all_tenants`` query. On a cloud without the
emulator's flavor and image, also set::

    export WALDUR_LIVE_OS_FLAVOR=<flavor name or ID>
    export WALDUR_LIVE_OS_IMAGE=<image name>
    export WALDUR_LIVE_OS_TIMEOUT=600   # seconds to wait for a server or volume

The unit tests mock the backend, so they cannot show that what Nova and Cinder
list for a tenant is what the import compares against, or that a server's
attached volume is imported once, with the server, and not again on its own.
"""

import os
import time
import unittest
import uuid
from unittest import mock

from cinderclient import exceptions as cinder_exceptions
from django.db.models import Q
from django.test import TransactionTestCase
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.common import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_core.core.enums import CoreStates
from waldur_core.core.utils import serialize_instance
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import tasks
from waldur_openstack import models as openstack_models
from waldur_openstack import tasks as openstack_tasks
from waldur_openstack.backend import OpenStackBackend, get_tenant_session
from waldur_openstack.session import (
    get_cinder_client,
    get_keystone_client,
    get_neutron_client,
    get_nova_client,
)
from waldur_openstack.tests import factories as openstack_factories

LIVE_ENV = (
    "WALDUR_LIVE_OS_AUTH_URL",
    "WALDUR_LIVE_OS_USERNAME",
    "WALDUR_LIVE_OS_PASSWORD",
    "WALDUR_LIVE_OS_PROJECT_NAME",
    "WALDUR_LIVE_OS_PROJECT_ID",
)

FLAVOR = os.environ.get("WALDUR_LIVE_OS_FLAVOR", "1")
IMAGE = os.environ.get("WALDUR_LIVE_OS_IMAGE", "cirros-0.6.2-x86_64")
TIMEOUT = int(os.environ.get("WALDUR_LIVE_OS_TIMEOUT", "30"))


def live_config():
    return {name: os.environ.get(name) for name in LIVE_ENV}


def run_import_inline(serialized_tenant):
    tasks.import_new_instances_and_volumes_of_tenant(serialized_tenant)


@unittest.skipUnless(
    all(os.environ.get(name) for name in LIVE_ENV),
    "live OpenStack credentials not configured; set %s" % ", ".join(LIVE_ENV),
)
class LiveBackgroundImportTest(TransactionTestCase):
    def setUp(self):
        super().setUp()
        cfg = live_config()
        self.suffix = uuid.uuid4().hex[:8]
        self.settings = openstack_factories.SettingsFactory(
            backend_url=cfg["WALDUR_LIVE_OS_AUTH_URL"],
            username=cfg["WALDUR_LIVE_OS_USERNAME"],
            password=cfg["WALDUR_LIVE_OS_PASSWORD"],
            options={"tenant_name": cfg["WALDUR_LIVE_OS_PROJECT_NAME"]},
            customer=structure_factories.CustomerFactory(),
            shared=True,
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(self.settings)
        self.tenant = openstack_factories.TenantFactory(
            name=f"background-import-{self.suffix}",
            service_settings=self.settings,
            project=structure_factories.ProjectFactory(),
            backend_id="",
            state=CoreStates.OK,
            user_username=cfg["WALDUR_LIVE_OS_USERNAME"],
            user_password=cfg["WALDUR_LIVE_OS_PASSWORD"],
        )
        self.backend.create_tenant(self.tenant)
        self.tenant.refresh_from_db()
        self.backend.add_admin_user_to_tenant(self.tenant)
        self.addCleanup(self.drop_project, self.tenant.backend_id)
        self.backend.pull_tenant_flavors(self.tenant)
        self.backend.pull_tenant_images(self.tenant)
        for offering_type in (OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING):
            marketplace_factories.OfferingFactory(scope=self.tenant, type=offering_type)

        session = get_tenant_session(self.tenant)
        self.nova = get_nova_client(session)
        self.cinder = get_cinder_client(session)
        self.neutron = get_neutron_client(session)
        self.servers, self.volumes, self.networks = [], [], []
        # Runs before drop_project: a real cloud deletes servers and volumes
        # asynchronously, and keeps a network while a port is still on it.
        self.addCleanup(self.drop_created)

    def drop_project(self, backend_id):
        # Neutron creates a default security group in every project and keeps
        # it after the project is gone.
        neutron = get_neutron_client(self.backend.admin_session)
        for group in neutron.list_security_groups(project_id=backend_id)[
            "security_groups"
        ]:
            try:
                neutron.delete_security_group(group["id"])
            except neutron_exceptions.NeutronClientException:
                pass
        try:
            get_keystone_client(self.backend.admin_session).projects.delete(backend_id)
        except keystone_exceptions.ClientException:
            pass

    def drop_created(self):
        """Best effort, in the order a real cloud needs; whatever could not be
        removed is reported at the end rather than stopping the rest."""
        left = []
        for server_id in self.servers:
            try:
                self.nova.servers.delete(server_id)
            except nova_exceptions.NotFound:
                continue
            except nova_exceptions.ClientException:
                pass
            if not self.is_gone(self.nova.servers.get, server_id, nova_exceptions):
                left.append(f"server {server_id}")
        for volume_id in self.volumes:
            try:
                # A volume detaches only once its server is gone.
                self.wait_for(
                    lambda: self.cinder.volumes.get(volume_id), "available", fail=False
                )
                self.cinder.volumes.delete(volume_id)
            except cinder_exceptions.NotFound:
                continue
            except cinder_exceptions.ClientException:
                pass
            if not self.is_gone(self.cinder.volumes.get, volume_id, cinder_exceptions):
                left.append(f"volume {volume_id}")
        for network_id in self.networks:
            try:
                self.neutron.delete_network(network_id)
            except neutron_exceptions.NotFound:
                pass
            except neutron_exceptions.NeutronClientException:
                left.append(f"network {network_id}")
        if left:
            self.fail("Could not clean up: " + ", ".join(left))

    def is_gone(self, get, backend_id, exceptions):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                get(backend_id)
            except exceptions.NotFound:
                return True
            time.sleep(2)
        return False

    def wait_until_gone(self, get, backend_id, exceptions):
        if not self.is_gone(get, backend_id, exceptions):
            self.fail(f"Timed out waiting for {backend_id} to be deleted")

    def wait_for(self, get, status, fail=True):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            current = get().status.lower()
            if current == status:
                return
            if current == "error" and fail:
                self.fail(f"Reached status error while waiting for {status}")
            time.sleep(2)
        if fail:
            self.fail(f"Timed out waiting for status {status}")

    def create_server_outside_waldur(self, name=None, wait=True):
        network = self.neutron.create_network(
            {"network": {"name": f"horizon-net-{self.suffix}"}}
        )["network"]
        self.networks.append(network["id"])
        self.neutron.create_subnet(
            {
                "subnet": {
                    "network_id": network["id"],
                    "ip_version": 4,
                    "cidr": "10.254.0.0/24",
                }
            }
        )
        image = openstack_models.Image.objects.get(settings=self.settings, name=IMAGE)
        flavor = openstack_models.Flavor.objects.get(
            Q(name=FLAVOR) | Q(backend_id=FLAVOR), settings=self.settings
        )
        server = self.nova.servers.create(
            name=name or f"horizon-server-{self.suffix}",
            image=image.backend_id,
            flavor=flavor.backend_id,
            nics=[{"net-id": network["id"]}],
        )
        self.servers.append(server.id)
        if wait:
            self.wait_for(lambda: self.nova.servers.get(server.id), "active")
        return server

    def create_volume_outside_waldur(self, name):
        volume = self.cinder.volumes.create(size=1, name=f"{name}-{self.suffix}")
        self.volumes.append(volume.id)
        self.wait_for(lambda: self.cinder.volumes.get(volume.id), "available")
        return volume

    def background_pull(self):
        """The hourly tenant resources pull, as the worker runs it, with the
        import it schedules run inline."""
        with mock.patch.object(
            tasks.import_new_instances_and_volumes_of_tenant,
            "delay",
            side_effect=run_import_inline,
        ):
            openstack_tasks.TenantResourcesPullTask().run(
                serialize_instance(self.tenant)
            )

    def resources_of(self, scope):
        return marketplace_models.Resource.objects.filter(scope=scope)

    def test_server_and_volume_created_outside_waldur_are_imported(self):
        server = self.create_server_outside_waldur()
        attached = self.create_volume_outside_waldur("attached")
        self.nova.volumes.create_server_volume(server.id, attached.id)
        self.wait_for(lambda: self.cinder.volumes.get(attached.id), "in-use")
        standalone = self.create_volume_outside_waldur("standalone")
        volume_ids = [attached.id, standalone.id]

        # The pull steps alone refresh only what Waldur already has.
        self.backend.pull_tenant_volumes(self.tenant)
        self.backend.pull_tenant_instances(self.tenant)
        self.assertFalse(
            openstack_models.Instance.objects.filter(backend_id=server.id).exists()
        )
        self.assertFalse(
            openstack_models.Volume.objects.filter(backend_id__in=volume_ids).exists()
        )

        self.background_pull()

        instance = openstack_models.Instance.objects.get(
            tenant=self.tenant, backend_id=server.id
        )
        self.assertEqual(instance.project, self.tenant.project)
        self.assertEqual(
            self.resources_of(instance).get().project_id, self.tenant.project_id
        )
        self.assertEqual(
            list(instance.volumes.values_list("backend_id", flat=True)),
            [attached.id],
        )
        for volume_id in volume_ids:
            volume = openstack_models.Volume.objects.get(
                tenant=self.tenant, backend_id=volume_id
            )
            self.assertEqual(
                self.resources_of(volume).get().offering.type,
                OPENSTACK_VOLUME_OFFERING,
            )

        self.background_pull()

        self.assertEqual(
            openstack_models.Instance.objects.filter(backend_id=server.id).count(), 1
        )
        self.assertEqual(self.resources_of(instance).count(), 1)
        for volume_id in volume_ids:
            volumes = openstack_models.Volume.objects.filter(backend_id=volume_id)
            self.assertEqual(volumes.count(), 1)
            self.assertEqual(self.resources_of(volumes.get()).count(), 1)

    def test_server_waldur_cannot_store_does_not_hold_back_the_rest(self):
        # OpenStack allows a 255-character server name; a marketplace resource
        # name holds 150, so the database rejects this one.
        server = self.create_server_outside_waldur(name="s" * 200, wait=False)
        volume = self.create_volume_outside_waldur("after-the-server")

        self.background_pull()

        self.assertFalse(
            openstack_models.Instance.objects.filter(backend_id=server.id).exists()
        )
        self.assertTrue(
            self.resources_of(
                openstack_models.Volume.objects.get(
                    tenant=self.tenant, backend_id=volume.id
                )
            ).exists()
        )

    def test_volume_deleted_outside_waldur_is_not_terminated(self):
        volume = self.create_volume_outside_waldur("deleted")
        self.background_pull()
        imported = openstack_models.Volume.objects.get(
            tenant=self.tenant, backend_id=volume.id
        )

        self.cinder.volumes.delete(volume.id)
        self.wait_until_gone(self.cinder.volumes.get, volume.id, cinder_exceptions)
        self.background_pull()

        self.assertTrue(openstack_models.Volume.objects.filter(pk=imported.pk).exists())
        self.assertNotEqual(
            self.resources_of(imported).get().state, ResourceStates.TERMINATED
        )
