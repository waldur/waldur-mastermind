from django.core import mail
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME, tasks


class NotificationsTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = structure_fixtures.ProjectFixture()
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)

        self.order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes={
                'schedules': [
                    {
                        'start': '2019-01-03T00:00:00.000000Z',
                        'end': '2019-01-05T23:59:59.000000Z',
                    },
                ],
                'name': 'booking',
            },
            state=marketplace_models.Order.States.EXECUTING,
        )

        marketplace_utils.process_order(self.order, fixture.staff)

        self.resource = self.order.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()

    @freeze_time('2019-01-02')
    def test_send_notification_message_one_day_before_event(self):
        event_type = 'notification'
        structure_factories.NotificationFactory(key=f"booking.{event_type}")
        tasks.send_notifications_about_upcoming_bookings()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.order.created_by.email])
        self.assertEqual(mail.outbox[0].subject, 'Reminder about upcoming booking.')
        self.assertTrue('booking' in mail.outbox[0].body)

    @freeze_time('2019-01-01')
    def test_not_send_notification_message_more_one_day_before_event(self):
        tasks.send_notifications_about_upcoming_bookings()
        self.assertEqual(len(mail.outbox), 0)
