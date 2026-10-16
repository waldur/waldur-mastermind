from constance.test.unittest import override_config
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories


class OfferingDeletedEventTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    def test_deletion_is_logged_on_the_provider(self):
        offering_uuid = self.offering.uuid.hex
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.delete(factories.OfferingFactory.get_url(self.offering))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        event = Event.objects.get(event_type=EventType.MARKETPLACE_OFFERING_DELETED)
        self.assertEqual(event.context["offering_uuid"], offering_uuid)
        self.assertEqual(event.context["user_uuid"], self.fixture.staff.uuid.hex)
        self.assertEqual(
            [feed.scope for feed in Feed.objects.filter(event=event)],
            [self.fixture.customer],
        )

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_deletion_of_draft_offering_by_owner_is_logged(self):
        self.offering.state = OfferingStates.DRAFT
        self.offering.save()
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_OFFERING)
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.delete(factories.OfferingFactory.get_url(self.offering))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_OFFERING_DELETED
            ).exists()
        )

    def test_customer_with_offering_can_be_deleted(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.delete(
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_OFFERING_DELETED
            ).exists()
        )
