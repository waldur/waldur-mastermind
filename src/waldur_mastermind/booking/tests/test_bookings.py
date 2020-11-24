import mock
from dateutil.parser import parse as parse_datetime
from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.google.tests import factories as google_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME, calendar


@ddt
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

    @data('owner', 'staff')
    def test_user_can_sync_bookings_to_calendar(self, user):
        service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.offering.customer
        )
        google_factories.GoogleCredentialsFactory(
            service_provider=service_provider,
            calendar_token='calendar_token',
            calendar_refresh_token='calendar_refresh_token',
        )

        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            f'/api/marketplace-bookings/{self.offering.uuid.hex}/google_calendar_sync/'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_user_cannot_sync_bookings_to_calendar(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            f'/api/marketplace-bookings/{self.offering.uuid.hex}/google_calendar_sync/'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_google_calendar_sync_validators(self):
        marketplace_factories.ServiceProviderFactory(customer=self.offering.customer)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            f'/api/marketplace-bookings/{self.offering.uuid.hex}/google_calendar_sync/'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_mastermind.google.backend.build')
    @freeze_time('2020-02-20')
    def test_bookings_sync(self, mock_build):
        mock_build().events().list().execute().get.return_value = []
        service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.offering.customer
        )
        google_factories.GoogleCredentialsFactory(service_provider=service_provider)
        sync_bookings = calendar.SyncBookings(self.offering)

        need_to_add, need_to_delete, need_to_update = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 1)
        self.assertEqual(need_to_add[0].id, self.schedules[1]['id'])
        self.assertEqual(len(need_to_delete), 0)
        self.assertEqual(len(need_to_update), 0)

        mock_build().events().list().execute().get.return_value = [
            {
                'start': {'dateTime': self.schedules[1]['start']},
                'end': {'dateTime': self.schedules[1]['end']},
                'id': self.schedules[1]['id'],
            }
        ]

        self.resource_2.attributes['schedules'][0][
            'start'
        ] = '2020-03-02T02:00:00+03:00'
        self.resource_2.save()
        need_to_add, need_to_delete, need_to_update = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 0)
        self.assertEqual(len(need_to_delete), 0)
        self.assertEqual(len(need_to_update), 1)
        self.assertEqual(need_to_update[0].id, self.schedules[1]['id'])

        self.resource_2.attributes['schedules'][0][
            'start'
        ] = '2020-02-02T02:00:00+03:00'
        self.resource_2.save()
        need_to_add, need_to_delete, need_to_update = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 0)
        self.assertEqual(len(need_to_delete), 1)
        self.assertEqual(need_to_delete.pop(), self.schedules[1]['id'])
        self.assertEqual(len(need_to_update), 0)


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
