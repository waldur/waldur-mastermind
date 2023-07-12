from unittest import mock

from rest_framework import test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.notifications import tasks as notifications_tasks
from waldur_mastermind.notifications.tests import factories as notifications_factories

from ..utils import get_users_for_query


class UsersFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = marketplace_factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )

    def test_offering_and_customer_are_specified(self):
        owner = self.fixture.owner
        manager = self.fixture.manager
        users = get_users_for_query(
            {
                'customers': [self.fixture.customer],
                'offerings': [self.offering],
            }
        )
        self.assertIn(owner, users)
        self.assertIn(manager, users)

    def test_all_users(self):
        owner = self.fixture.owner
        manager = self.fixture.manager
        users = get_users_for_query(
            {
                'all_users': True,
            }
        )
        self.assertIn(owner, users)
        self.assertIn(manager, users)


class TaskTest(test.APITransactionTestCase):
    def setUp(self):
        self.emails_1 = ['email_%s@gmail.com' % i for i in range(1, 51)]
        self.emails_2 = ['email_%s@gmail.com' % i for i in range(51, 101)]
        self.emails_3 = ['email_%s@gmail.com' % i for i in range(101, 110)]
        self.broadcast = notifications_factories.BroadcastMessageFactory(
            query='', emails=self.emails_1 + self.emails_2 + self.emails_3
        )

    @mock.patch('waldur_mastermind.notifications.tasks.send_mail')
    def test_send_broadcast_message_email(self, send_mail_mock):
        notifications_tasks.send_broadcast_message_email(self.broadcast.uuid.hex)
        self.assertEqual(send_mail_mock.call_count, 3)
        self.assertEqual(send_mail_mock.call_args_list[0].kwargs['bcc'], self.emails_1)

        self.assertEqual(send_mail_mock.call_args_list[1].kwargs['bcc'], self.emails_2)

        self.assertEqual(send_mail_mock.call_args_list[2].kwargs['bcc'], self.emails_3)
