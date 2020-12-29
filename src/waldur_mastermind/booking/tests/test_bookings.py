from dateutil.parser import parse as parse_datetime
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME


class BookingsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer, type=PLUGIN_NAME
        )
        self.schedules = [
            {
                'start': '2020-02-12T02:00:00+03:00',
                'end': '2020-02-15T02:00:00+03:00',
                'id': '123',
            },
            {
                'start': '2020-03-01T02:00:00+03:00',
                'end': '2020-03-05T02:00:00+03:00',
                'id': '456',
            },
        ]

        self.resource_1 = marketplace_factories.ResourceFactory(
            offering=self.offering,
            state=marketplace_models.Resource.States.OK,
            attributes={'schedules': [self.schedules[0]]},
        )

        self.resource_2 = marketplace_factories.ResourceFactory(
            offering=self.offering,
            state=marketplace_models.Resource.States.OK,
            attributes={'schedules': [self.schedules[1]]},
        )

    def test_offering_bookings(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            f'/api/marketplace-bookings/{self.offering.uuid.hex}/'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(
            response.data,
            [
                {
                    'start': parse_datetime('2020-02-12T02:00:00+03:00'),
                    'end': parse_datetime('2020-02-15T02:00:00+03:00'),
                },
                {
                    'start': parse_datetime('2020-03-01T02:00:00+03:00'),
                    'end': parse_datetime('2020-03-05T02:00:00+03:00'),
                },
            ],
        )


@freeze_time('2020-02-01')
class SlotsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        schedules = [
            {'start': '2020-01-01T00:00:00.000Z', 'end': '2020-01-02T00:00:00.000Z'},
            {'start': '2020-03-01T00:00:00.000Z', 'end': '2020-03-02T00:00:00.000Z'},
        ]
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer,
            type=PLUGIN_NAME,
            attributes={'schedules': schedules,},
        )
        self.url = marketplace_factories.OfferingFactory.get_url(self.offering)

    def test_do_not_display_expired_slots(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['attributes']['schedules']), 1)

    def test_change_start_if_end_is_not_expired(self):
        schedules = [
            {'start': '2020-01-01T00:00:00.000Z', 'end': '2020-02-10T00:00:00.000Z'},
        ]
        self.offering.attributes = {
            'schedules': schedules,
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['attributes']['schedules']), 1)
        self.assertEqual(
            response.data['attributes']['schedules'][0]['start'],
            '2020-02-01T00:00:00.000Z',
        )
