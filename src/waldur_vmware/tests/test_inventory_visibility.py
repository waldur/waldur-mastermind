from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from . import factories

INVENTORY_FACTORIES = (
    factories.TemplateFactory,
    factories.ClusterFactory,
    factories.NetworkFactory,
    factories.DatastoreFactory,
    factories.FolderFactory,
)


class VMwareInventoryVisibilityTest(test.APITestCase):
    """The inventory of a VMware settings object is shown to whom may use it:
    its provider, consumers who may order from its offering or see a VM on
    it, and staff and support."""

    def setUp(self):
        self.provider = structure_fixtures.ProjectFixture()
        self.settings = factories.VMwareServiceSettingsFactory(
            shared=False, customer=self.provider.customer
        )
        self.rows = {
            factory: factory(settings=self.settings) for factory in INVENTORY_FACTORIES
        }

    def _visible(self, user):
        """The inventory views that list and retrieve this settings' row."""
        self.client.force_authenticate(user)
        visible = []
        for factory, row in self.rows.items():
            listed = self.client.get(
                factory.get_list_url(), {"settings_uuid": self.settings.uuid.hex}
            )
            self.assertEqual(listed.status_code, status.HTTP_200_OK)
            uuids = [item["uuid"] for item in listed.json()]
            retrieved = self.client.get(factory.get_url(row)).status_code
            if row.uuid.hex in uuids:
                self.assertEqual(retrieved, status.HTTP_200_OK, factory)
                visible.append(factory)
            else:
                self.assertEqual(retrieved, status.HTTP_404_NOT_FOUND, factory)
        return visible

    def test_user_with_no_way_to_use_the_settings_sees_nothing(self):
        self.assertEqual(self._visible(structure_factories.UserFactory()), [])

    def test_owner_of_another_organization_sees_nothing(self):
        self.assertEqual(self._visible(structure_fixtures.ProjectFixture().owner), [])

    def test_provider_organization_sees_its_inventory(self):
        self.assertEqual(self._visible(self.provider.owner), list(INVENTORY_FACTORIES))

    def test_consumer_who_may_order_sees_the_inventory(self):
        offering = marketplace_factories.OfferingFactory(
            scope=self.settings,
            shared=True,
            state=OfferingStates.ACTIVE,
            customer=self.provider.customer,
        )
        marketplace_factories.PlanFactory(offering=offering)
        consumer = structure_fixtures.ProjectFixture()

        self.assertEqual(self._visible(consumer.owner), list(INVENTORY_FACTORIES))

    def test_consumer_who_may_order_sees_no_other_settings_inventory(self):
        offering = marketplace_factories.OfferingFactory(
            scope=self.settings,
            shared=True,
            state=OfferingStates.ACTIVE,
            customer=self.provider.customer,
        )
        marketplace_factories.PlanFactory(offering=offering)
        other_settings = factories.VMwareServiceSettingsFactory(shared=False)
        other_rows = [
            factory(settings=other_settings) for factory in INVENTORY_FACTORIES
        ]
        self.client.force_authenticate(structure_fixtures.ProjectFixture().owner)

        for factory, row in zip(INVENTORY_FACTORIES, other_rows):
            with self.subTest(factory=factory):
                listed = [
                    item["uuid"]
                    for item in self.client.get(factory.get_list_url()).json()
                ]
                self.assertIn(self.rows[factory].uuid.hex, listed)
                self.assertNotIn(row.uuid.hex, listed)

    def test_offering_of_another_scope_type_does_not_match_settings_by_id(self):
        # An offering whose scope is not a service settings object, with the
        # same object id as these settings.
        offering = marketplace_factories.OfferingFactory(
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
            object_id=self.settings.id,
            shared=True,
            state=OfferingStates.ACTIVE,
        )
        marketplace_factories.PlanFactory(offering=offering)

        self.assertEqual(self._visible(structure_fixtures.ProjectFixture().owner), [])

    def test_consumer_of_an_archived_offering_sees_nothing(self):
        offering = marketplace_factories.OfferingFactory(
            scope=self.settings,
            shared=True,
            state=OfferingStates.ARCHIVED,
            customer=self.provider.customer,
        )
        marketplace_factories.PlanFactory(offering=offering)

        self.assertEqual(self._visible(structure_fixtures.ProjectFixture().owner), [])

    def test_user_who_sees_a_vm_on_the_settings_sees_the_inventory(self):
        consumer = structure_fixtures.ProjectFixture()
        factories.VirtualMachineFactory(
            service_settings=self.settings,
            project=consumer.project,
            template=self.rows[factories.TemplateFactory],
            cluster=self.rows[factories.ClusterFactory],
        )

        self.assertEqual(self._visible(consumer.member), list(INVENTORY_FACTORIES))

    def test_staff_and_support_see_every_inventory(self):
        for user in (self.provider.staff, self.provider.global_support):
            with self.subTest(user=user):
                self.assertEqual(self._visible(user), list(INVENTORY_FACTORIES))
