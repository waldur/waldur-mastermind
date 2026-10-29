import importlib
from unittest import mock

from django.apps import apps
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack import models

from . import factories, fixtures

cascade_migration = importlib.import_module(
    "waldur_openstack.migrations.0089_move_tenant_objects_to_tenant_project"
)


class TenantMoveTest(test.APITestCase):
    """A tenant's networks, ports, routers and the rest are the tenant's: they
    go where it goes. Permission checks and listings read their own project."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.old_project = self.fixture.project
        scoped = dict(
            service_settings=self.fixture.settings,
            project=self.old_project,
            state=CoreStates.OK,
        )
        self.load_balancer = factories.LoadBalancerFactory(tenant=self.tenant, **scoped)
        self.pool = factories.PoolFactory(load_balancer=self.load_balancer, **scoped)
        self.objects = [
            self.fixture.network,
            self.fixture.subnet,
            self.fixture.port,
            self.fixture.router,
            self.fixture.floating_ip,
            self.fixture.security_group,
            self.fixture.server_group,
            self.load_balancer,
            self.pool,
            factories.ListenerFactory(load_balancer=self.load_balancer, **scoped),
            factories.PoolMemberFactory(pool=self.pool, **scoped),
            factories.HealthMonitorFactory(pool=self.pool, **scoped),
        ]
        # Marketplace resources of their own, moved on their own.
        self.own_resources = [self.fixture.instance, self.fixture.volume]
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant, project=self.old_project
        )
        self.new_project = structure_factories.ProjectFactory()

    def assert_in_project(self, objects, project):
        for obj in objects:
            with self.subTest(obj=type(obj).__name__):
                obj.refresh_from_db()
                self.assertEqual(obj.project, project)

    def test_its_objects_move_with_it(self):
        marketplace_utils.move_resource(self.resource, self.new_project)

        self.assert_in_project(self.objects, self.new_project)
        self.assert_in_project(self.own_resources, self.old_project)

    def test_any_change_of_its_project_moves_them(self):
        self.tenant.project = self.new_project
        self.tenant.save()

        self.assert_in_project(self.objects, self.new_project)

    @mock.patch("waldur_openstack.executors.NetworkUpdateExecutor.execute")
    def test_the_new_project_manages_them_and_the_old_one_no_longer_sees_them(
        self, execute
    ):
        old_admin = self.fixture.admin
        new_admin = structure_factories.UserFactory()
        self.new_project.add_user(new_admin, ProjectRole.ADMIN)

        marketplace_utils.move_resource(self.resource, self.new_project)

        url = factories.NetworkFactory.get_url(self.fixture.network)
        self.client.force_authenticate(old_admin)
        refused = self.client.patch(url, {"name": "renamed"}, format="json")
        self.client.force_authenticate(new_admin)
        renamed = self.client.patch(url, {"name": "renamed"}, format="json")

        self.assertEqual(refused.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(renamed.status_code, status.HTTP_200_OK, renamed.data)

    def test_objects_of_another_tenant_stay(self):
        other_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=self.old_project,
            backend_id="other-tenant",
        )
        other_network = factories.NetworkFactory(
            service_settings=self.fixture.settings,
            project=self.old_project,
            tenant=other_tenant,
        )
        # A port another tenant made on the moved tenant's shared network.
        shared_port = factories.PortFactory(
            network=self.fixture.network,
            tenant=other_tenant,
            project=self.old_project,
            service_settings=self.fixture.settings,
        )

        marketplace_utils.move_resource(self.resource, self.new_project)

        self.assert_in_project([other_network, shared_port], self.old_project)

    def test_migration_moves_objects_of_tenants_moved_before(self):
        # What a move left behind before its objects went with the tenant.
        models.Tenant.objects.filter(pk=self.tenant.pk).update(project=self.new_project)

        cascade_migration.move_tenant_objects_to_tenant_project(apps, None)

        self.assert_in_project(self.objects, self.new_project)
        self.assert_in_project(self.own_resources, self.old_project)
