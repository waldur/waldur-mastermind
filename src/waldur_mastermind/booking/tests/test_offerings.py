import mock
from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.google.tests import factories as google_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME, calendar


@ddt
class BookingOfferingActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer,
            type=PLUGIN_NAME,
            state=marketplace_models.Offering.States.ACTIVE,
        )
        marketplace_factories.PlanFactory(offering=self.offering)
        self.google_calendar = google_factories.GoogleCalendarFactory(
            offering=self.offering
        )
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.offering.customer
        )
        self.google_credentials = google_factories.GoogleCredentialsFactory(
            service_provider=self.service_provider,
            calendar_token='calendar_token',
            calendar_refresh_token='calendar_refresh_token',
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

    @data('owner', 'staff')
    def test_user_can_sync_bookings_to_calendar(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            f'/api/booking-offerings/{self.offering.uuid.hex}/google_calendar_sync/'
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('user')
    def test_user_cannot_sync_bookings_to_calendar(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            f'/api/booking-offerings/{self.offering.uuid.hex}/google_calendar_sync/'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_google_calendar_sync_validators(self):
        self.google_credentials.delete()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            f'/api/booking-offerings/{self.offering.uuid.hex}/google_calendar_sync/'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_mastermind.google.backend.build')
    @freeze_time('2020-02-20')
    def test_bookings_sync(self, mock_build):
        mock_build().events().list().execute().get.return_value = []
        sync_bookings = calendar.SyncBookings(self.offering)

        need_to_add, need_to_delete, need_to_update = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 2)
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
        self.assertEqual(len(need_to_add), 1)
        self.assertEqual(len(need_to_delete), 0)
        self.assertEqual(len(need_to_update), 1)
        self.assertEqual(need_to_update[0].id, self.schedules[1]['id'])

        # Past events are also being updated
        self.resource_2.attributes['schedules'][0][
            'start'
        ] = '2020-02-02T02:00:00+03:00'
        self.resource_2.save()
        need_to_add, need_to_delete, need_to_update = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 1)
        self.assertEqual(len(need_to_delete), 0)
        self.assertEqual(len(need_to_update), 1)
        self.assertEqual(need_to_update[0].id, self.schedules[1]['id'])

    @mock.patch('waldur_mastermind.google.backend.build')
    def test_automatically_create_google_calendar(self, mock_build):
        # if calendar backend_id exists
        backend = calendar.SyncBookings(self.offering)
        backend.calendar_id
        mock_build().calendars().insert().execute.assert_not_called()

        # if calendar backend_id doesn't exist
        self.google_calendar.backend_id = ''
        self.google_calendar.save()
        backend = calendar.SyncBookings(self.offering)
        mock_build().calendars().insert().execute.return_value = {
            'id': 'new_calendar_id'
        }
        backend.calendar_id
        mock_build().calendars().insert().execute.assert_called_once()
        self.google_calendar.refresh_from_db()
        self.assertEqual(self.google_calendar.backend_id, 'new_calendar_id')

    @mock.patch('waldur_mastermind.booking.handlers.GoogleCalendarRenameExecutor')
    def test_update_google_calendar_name_if_offering_name_has_been_updated(
        self, mock_executor
    ):
        self.offering.name = 'new name'
        self.offering.save()
        mock_executor.execute.assert_called_once()

    def test_marketplace_offering_serializer_has_calendar_info(self):
        self.client.force_authenticate(self.fixture.staff)

        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('google_calendar_is_public' in response.data.keys())
        self.assertEqual(
            response.data['google_calendar_is_public'], self.google_calendar.public
        )

        url = marketplace_factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('google_calendar_is_public' in response.data.keys())
        self.assertEqual(
            response.data['google_calendar_is_public'], self.google_calendar.public
        )

    def test_marketplace_offering_serializer_has_google_calendar_link(self):
        self.client.force_authenticate(self.fixture.staff)

        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('google_calendar_link' in response.data.keys())
        self.assertEqual(
            response.data['google_calendar_link'], self.google_calendar.http_link
        )

        url = marketplace_factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('google_calendar_link' in response.data.keys())
        self.assertEqual(
            response.data['google_calendar_link'], self.google_calendar.http_link
        )
