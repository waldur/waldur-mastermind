import importlib

from django.apps import apps
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack import models as openstack_models
from waldur_openstack.tests import fixtures as openstack_fixtures

offering_migration = importlib.import_module(
    "waldur_mastermind.marketplace_openstack.migrations."
    "0021_move_tenant_offerings_to_tenant_project"
)


class TenantMoveOfferingsTest(test.APITestCase):
    """A tenant's own instance and volume offerings are private to its project
    and organization: they go where the tenant goes."""

    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.old_project = self.fixture.project
        self.offerings = [
            marketplace_factories.OfferingFactory(
                type=offering_type,
                scope=self.tenant,
                shared=False,
                customer=self.old_project.customer,
                project=self.old_project,
            )
            for offering_type in (
                OPENSTACK_INSTANCE_OFFERING,
                OPENSTACK_VOLUME_OFFERING,
            )
        ]
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant, project=self.old_project
        )
        # Another organization's project.
        self.new_project = structure_factories.ProjectFactory()

    def assert_offerings_in(self, project):
        for offering in self.offerings:
            with self.subTest(offering=offering.type):
                offering.refresh_from_db()
                self.assertEqual(offering.project, project)
                self.assertEqual(offering.customer, project.customer)

    def test_they_move_with_the_tenant(self):
        marketplace_utils.move_resource(self.resource, self.new_project)

        self.assert_offerings_in(self.new_project)

    def test_migration_moves_those_of_tenants_moved_before(self):
        openstack_models.Tenant.objects.filter(pk=self.tenant.pk).update(
            project=self.new_project
        )

        offering_migration.move_tenant_offerings_to_tenant_project(apps, None)

        self.assert_offerings_in(self.new_project)
