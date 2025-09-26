from unittest import mock

from rest_framework import test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.notifications import tasks as notifications_tasks
from waldur_mastermind.notifications.tests import factories as notifications_factories
from waldur_mastermind.notifications.utils import (
    get_mapping,
    get_recipients_for_query,
    get_user_emails_for_query,
)


class BroadcastQueryTest(test.APITransactionTestCase):
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
        self.owner = self.fixture.owner
        self.manager = self.fixture.manager
        self.notification_emails_string = "admin@acme.com, support@acme.com"

    def test_offering_and_customer_are_specified(self):
        emails = get_user_emails_for_query(
            {
                "customers": [self.fixture.customer],
                "offerings": [self.offering],
            }
        )
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)

    def test_all_users(self):
        emails = get_user_emails_for_query(
            {
                "all_users": True,
            }
        )
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)

    def test_notification_emails_included_for_all_users_targeting(self):
        """Test that notification emails are included when targeting all users"""
        self.fixture.customer.notification_emails = self.notification_emails_string
        self.fixture.customer.save()

        emails = get_user_emails_for_query(
            {
                "all_users": True,
            }
        )

        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

    def test_notification_emails_included_for_structure_unit_targeting(self):
        """Test that notification emails are included alongside users when targeting offerings"""
        self.fixture.customer.notification_emails = self.notification_emails_string
        self.fixture.customer.save()

        emails = get_user_emails_for_query(
            {
                "customers": [self.fixture.customer],
                "offerings": [self.offering],
            }
        )

        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)

        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

    def test_notification_emails_included_for_direct_customer_targeting(self):
        """Test that notification emails ARE included when targeting customers directly"""
        self.fixture.customer.notification_emails = self.notification_emails_string
        self.fixture.customer.save()

        emails = get_user_emails_for_query(
            {
                "customers": [self.fixture.customer],
            }
        )

        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)


class BroadcastMappingTest(test.APITransactionTestCase):
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
        self.owner = self.fixture.owner
        self.manager = self.fixture.manager
        self.notification_emails_string = "admin@acme.com, support@acme.com"
        self.fixture.customer.notification_emails = self.notification_emails_string
        self.fixture.customer.save()

    def test_mapping_includes_notification_emails_for_structure_unit_targeting(self):
        """Test that get_mapping includes info contacts for structure unit targeting"""

        users, user_offerings, user_customers, notification_emails = get_mapping(
            {
                "customers": [self.fixture.customer],
                "offerings": [self.offering],
            }
        )

        self.assertIn(self.owner.id, users)
        self.assertIn(self.manager.id, users)

        self.assertIn("admin@acme.com", notification_emails)
        self.assertIn("support@acme.com", notification_emails)

        admin_contact = notification_emails["admin@acme.com"]
        self.assertEqual(admin_contact["email"], "admin@acme.com")
        self.assertEqual(admin_contact["customer"], self.fixture.customer)
        self.assertEqual(len(admin_contact["offerings"]), 1)
        self.assertEqual(admin_contact["offerings"][0], self.offering)

    def test_mapping_includes_notification_emails_for_direct_customer_targeting(self):
        """Test that get_mapping includes notification emails for direct customer targeting"""

        users, _, _, notification_emails = get_mapping(
            {
                "customers": [self.fixture.customer],
            }
        )

        self.assertIn(self.owner.id, users)
        self.assertIn(self.manager.id, users)

        self.assertIn("admin@acme.com", notification_emails)
        self.assertIn("support@acme.com", notification_emails)


class BroadcastRecipientsTest(test.APITransactionTestCase):
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
        self.owner = self.fixture.owner
        self.manager = self.fixture.manager
        self.notification_emails_string = "admin@acme.com, support@acme.com"
        self.fixture.customer.notification_emails = self.notification_emails_string
        self.fixture.customer.save()

    def test_recipients_includes_notification_emails_for_structure_unit_targeting(self):
        """Test that get_recipients_for_query includes notification emails for structure unit targeting"""

        recipients = get_recipients_for_query(
            {
                "customers": [self.fixture.customer],
                "offerings": [self.offering],
            }
        )

        emails = [recipient["email"] for recipient in recipients]

        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)

        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

        notification_email_recipients = [
            r
            for r in recipients
            if r["email"] in ["admin@acme.com", "support@acme.com"]
        ]
        for recipient in notification_email_recipients:
            self.assertTrue(recipient["full_name"].startswith("Notification email for"))
            self.assertIn(self.fixture.customer.name, recipient["full_name"])
            self.assertEqual(len(recipient["offerings"]), 1)
            self.assertEqual(recipient["offerings"][0]["uuid"], self.offering.uuid)

    def test_recipients_includes_notification_emails_for_direct_customer_targeting(
        self,
    ):
        """Test that get_recipients_for_query includes notification emails for direct customer targeting"""

        recipients = get_recipients_for_query(
            {
                "customers": [self.fixture.customer],
            }
        )

        emails = [recipient["email"] for recipient in recipients]

        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

    def test_recipients_includes_notification_emails_for_all_users_targeting(self):
        """Test that get_recipients_for_query includes notification emails for all users targeting"""

        recipients = get_recipients_for_query(
            {
                "all_users": True,
            }
        )

        emails = [recipient["email"] for recipient in recipients]

        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

        notification_email_recipients = [
            r
            for r in recipients
            if r["email"] in ["admin@acme.com", "support@acme.com"]
        ]
        for recipient in notification_email_recipients:
            self.assertTrue(recipient["full_name"].startswith("Notification email for"))
            self.assertIn(self.fixture.customer.name, recipient["full_name"])
            self.assertEqual(len(recipient["offerings"]), 0)


class BroadcastAPITest(test.APITransactionTestCase):
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
        self.owner = self.fixture.owner
        self.manager = self.fixture.manager
        self.notification_emails_string = "admin@acme.com, support@acme.com"
        self.client.force_authenticate(self.fixture.staff)
        self.fixture.customer.notification_emails = self.notification_emails_string
        self.fixture.customer.save()

    def test_recipients_endpoint_includes_notification_emails_for_structure_unit_targeting(
        self,
    ):
        """Test that /recipients/ endpoint includes info contacts for structure unit targeting"""

        response = self.client.get(
            "/api/broadcast-messages/recipients/",
            {
                "customers": self.fixture.customer.uuid.hex,
                "offerings": self.offering.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, 200)
        recipients = response.data

        emails = [recipient["email"] for recipient in recipients]
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

        notification_email_recipients = next(
            r for r in recipients if r["email"] == "admin@acme.com"
        )
        self.assertEqual(
            notification_email_recipients["full_name"],
            f"Notification email for {self.fixture.customer.name}",
        )

    def test_recipients_endpoint_includes_notification_emails_for_direct_customer_targeting(
        self,
    ):
        """Test that /recipients/ endpoint includes notification emails for direct customer targeting"""

        response = self.client.get(
            "/api/broadcast-messages/recipients/",
            {
                "customers": self.fixture.customer.uuid.hex,
            },
        )

        self.assertEqual(response.status_code, 200)
        recipients = response.data

        emails = [recipient["email"] for recipient in recipients]
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

    def test_create_broadcast_includes_notification_emails_for_structure_unit_targeting(
        self,
    ):
        """Test that creating a broadcast includes notification emails for structure unit targeting"""

        # Create broadcast with structure unit targeting
        response = self.client.post(
            "/api/broadcast-messages/",
            {
                "subject": "Test Broadcast",
                "body": "Test message",
                "query": {
                    "customers": [self.fixture.customer.uuid.hex],
                    "offerings": [self.offering.uuid.hex],
                },
            },
        )

        self.assertEqual(response.status_code, 201)
        broadcast_data = response.data

        emails = broadcast_data["emails"]
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

    def test_create_broadcast_includes_notification_emails_for_direct_customer_targeting(
        self,
    ):
        """Test that creating a broadcast includes notification emails for direct customer targeting"""
        # Create broadcast with direct customer targeting
        response = self.client.post(
            "/api/broadcast-messages/",
            {
                "subject": "Test Broadcast",
                "body": "Test message",
                "query": {
                    "customers": [self.fixture.customer.uuid.hex],
                },
            },
        )

        self.assertEqual(response.status_code, 201)
        broadcast_data = response.data

        # Check that emails include both users AND notification emails
        emails = broadcast_data["emails"]
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

    def test_recipients_endpoint_includes_notification_emails_for_all_users_targeting(
        self,
    ):
        """Test that /recipients/ endpoint includes notification emails for all users targeting"""

        response = self.client.get(
            "/api/broadcast-messages/recipients/",
            {
                "all_users": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        recipients = response.data

        emails = [recipient["email"] for recipient in recipients]
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)

        notification_email_recipients = next(
            r for r in recipients if r["email"] == "admin@acme.com"
        )
        self.assertEqual(
            notification_email_recipients["full_name"],
            f"Notification email for {self.fixture.customer.name}",
        )
        self.assertEqual(len(notification_email_recipients["offerings"]), 0)

    def test_create_broadcast_includes_notification_emails_for_all_users_targeting(
        self,
    ):
        """Test that creating a broadcast includes notification emails for all users targeting"""

        response = self.client.post(
            "/api/broadcast-messages/",
            {
                "subject": "Test Broadcast",
                "body": "Test message",
                "query": {
                    "all_users": True,
                },
            },
        )

        self.assertEqual(response.status_code, 201)
        broadcast_data = response.data

        emails = broadcast_data["emails"]
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.manager.email, emails)
        self.assertIn("admin@acme.com", emails)
        self.assertIn("support@acme.com", emails)


class BroadcastTaskTest(test.APITransactionTestCase):
    def setUp(self):
        self.emails_1 = ["email_%s@gmail.com" % i for i in range(1, 51)]
        self.emails_2 = ["email_%s@gmail.com" % i for i in range(51, 101)]
        self.emails_3 = ["email_%s@gmail.com" % i for i in range(101, 110)]
        self.broadcast = notifications_factories.BroadcastMessageFactory(
            query="", emails=self.emails_1 + self.emails_2 + self.emails_3
        )

    @mock.patch("waldur_mastermind.notifications.tasks.send_mail")
    def test_send_broadcast_message_email(self, send_mail_mock):
        notifications_tasks.send_broadcast_message_email(self.broadcast.uuid.hex)
        self.assertEqual(send_mail_mock.call_count, 3)
        self.assertEqual(send_mail_mock.call_args_list[0].kwargs["bcc"], self.emails_1)

        self.assertEqual(send_mail_mock.call_args_list[1].kwargs["bcc"], self.emails_2)

        self.assertEqual(send_mail_mock.call_args_list[2].kwargs["bcc"], self.emails_3)
