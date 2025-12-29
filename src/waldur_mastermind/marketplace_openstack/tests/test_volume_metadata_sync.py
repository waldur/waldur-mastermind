from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack.tests.utils import BaseOpenStackTest
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures


class VolumeMetadataSyncTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.volume = self.fixture.volume
        # Create resource without scope initially
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=marketplace_factories.OfferingFactory(type="OpenStack.Volume"),
            scope=None,
        )

    def test_metadata_populated_when_scope_assigned(self):
        # Ensure volume has size
        self.volume.size = 100
        self.volume.save()

        # Assign scope
        self.resource.scope = self.volume
        self.resource.save()

        self.resource.refresh_from_db()

        # Check if metadata is populated
        self.assertEqual(self.resource.backend_metadata.get("size"), 100)

    def test_metadata_populated_when_volume_created_for_existing_resource(self):
        # Create resource first
        resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=marketplace_factories.OfferingFactory(type="OpenStack.Volume"),
            scope=None,
        )

        # Create volume and assign it to resource (simulation of some flows)
        # Note: In standard flow, if we assign scope via save(), it should trigger.
        volume = openstack_factories.VolumeFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            size=200,
        )

        resource.scope = volume
        resource.save()

        resource.refresh_from_db()
        self.assertEqual(resource.backend_metadata.get("size"), 200)
