from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models

from .. import tasks
from . import fixtures


@freeze_time("2020-02-01")
class TaskTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.BookingFixture()
        self.fixture.order.state = marketplace_models.Order.States.EXECUTING
        self.fixture.order.save()

    def test_reject_past_booking(self):
        self.fixture.resource.attributes["schedules"] = [
            {
                "start": "2020-01-01T02:00:00+03:00",
                "end": "2020-01-15T02:00:00+03:00",
                "id": "1",
            },
            {
                "start": "2020-01-16T02:00:00+03:00",
                "end": "2020-01-17T02:00:00+03:00",
                "id": "2",
            },
        ]
        self.fixture.resource.save()
        tasks.reject_past_bookings()
        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.TERMINATED
        )

    def test_do_not_reject_actual_booking(self):
        self.fixture.resource.attributes["schedules"] = [
            {
                "start": "2020-01-01T02:00:00+03:00",
                "end": "2020-01-15T02:00:00+03:00",
                "id": "1",
            },
            {
                "start": "2020-03-01T02:00:00+03:00",
                "end": "2020-03-15T02:00:00+03:00",
                "id": "2",
            },
        ]
        self.fixture.resource.save()
        tasks.reject_past_bookings()
        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.CREATING
        )
