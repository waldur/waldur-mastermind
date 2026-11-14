"""The regular project usage update, sent as openportal.project_usage_update."""

import datetime
from unittest import mock

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Notification
from waldur_core.structure.notifications import NOTIFICATIONS
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoice_models
from waldur_openportal import models, tasks

KEY = "openportal.project_usage_update"


def _days(n):
    return datetime.timedelta(days=n)


@override_config(
    SITE_NAME="Example HPC",
    DOCS_URL="https://docs.example.com",
    SUPPORT_PORTAL_URL="https://support.example.com",
)
class ProjectUsageUpdateTest(TestCase):
    def setUp(self):
        # Judged against the real local date, as Project.is_expired is.
        self.now = timezone.localtime().replace(hour=12, minute=0)
        self.today = self.now.date()

        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.project.end_date = self.today + _days(40)
        self.project.grace_period_days = 30
        self.project.save()
        self.member = self.fixture.admin

        models.Allocation.objects.create(
            name="local",
            project=self.project,
            service_settings=structure_factories.ServiceSettingsFactory(
                customer=self.fixture.customer
            ),
            node_usage=125,
        )
        invoice_models.CustomerCredit.objects.create(
            customer=self.fixture.customer, value=2000
        )
        invoice_models.ProjectCredit.objects.create(project=self.project, value=1000)
        self.notification = Notification.objects.create(key=KEY, enabled=True)

    def send(self, now=None):
        return tasks.send_project_updates(now or self.now)

    def message(self):
        self.assertEqual(self.send(), 1)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        return message, message.body, message.alternatives[0][0]

    def test_it_is_a_registered_notification(self):
        paths = [item["path"] for item in NOTIFICATIONS["openportal"]]
        self.assertIn("project_usage_update", paths)

    def test_the_task_sends_it(self):
        with mock.patch.object(tasks, "send_project_updates") as send:
            tasks.send_notifications()
        send.assert_called_once()

    def test_it_is_sent_to_each_member_with_the_usage(self):
        message, text, html = self.message()
        self.assertEqual(message.to, [self.member.email])
        self.assertIn(
            "To date, 125.00 node hours have been used, leaving 875.00 remaining",
            text,
        )
        self.assertIn("875.00", html)

    def test_it_names_the_configured_site_not_a_fixed_one(self):
        message, text, html = self.message()
        self.assertTrue(message.subject.startswith("Example HPC project status update"))
        self.assertIn('your Example HPC project "%s"' % self.project.name, text)
        self.assertIn(f"/projects/{self.project.uuid.hex}/", text)
        self.assertIn("https://docs.example.com", text)
        self.assertIn("https://support.example.com", text)

    @override_config(SITE_NAME="Another Site")
    def test_the_site_name_follows_the_configuration(self):
        message, text, html = self.message()
        self.assertTrue(
            message.subject.startswith("Another Site project status update")
        )
        self.assertIn('your Another Site project "%s"' % self.project.name, text)
        for body in (message.subject, text, html):
            self.assertNotIn("Example HPC", body)

    @override_config(DOCS_URL="", SUPPORT_PORTAL_URL="")
    def test_docs_and_support_lines_are_left_out_when_not_configured(self):
        _, text, html = self.message()
        self.assertNotIn("documentation", text)
        self.assertNotIn("raise a ticket", text)
        self.assertNotIn("documentation", html)
        self.assertIn("please ask the project PI to raise a request.", text)

    def test_data_is_deleted_after_the_grace_period_not_at_the_end_date(self):
        _, text, html = self.message()
        end = self.project.end_date
        deletion = end + _days(30)
        self.assertIn(f"before {end.strftime('%-d %B %Y')}, which is in 40 days", text)
        self.assertIn("grace period of 30 days", text)
        self.assertIn(
            f"All remaining data will be deleted on {deletion.strftime('%-d %B %Y')}",
            text,
        )
        self.assertNotIn("won't be able to log in after your project ends", text)
        self.assertIn(deletion.strftime("%-d %B %Y"), html)

    def test_without_a_grace_period_data_is_deleted_when_the_project_ends(self):
        self.project.grace_period_days = 0
        self.project.save()
        _, text, _ = self.message()
        self.assertNotIn("grace period", text)
        self.assertIn(
            "You must copy back all data before this date. You won't be able to "
            "log in after your project ends and all remaining data will be deleted.",
            text,
        )

    def test_nothing_is_sent_or_recorded_while_the_notification_is_disabled(self):
        self.notification.enabled = False
        self.notification.save()
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(
            models.ProjectNotification.objects.filter(
                project=self.project, last_notification__isnull=False
            ).exists()
        )

    def test_it_is_sent_once_per_period(self):
        self.assertEqual(self.send(), 1)
        self.assertEqual(self.send(self.now + _days(13)), 0)
        self.assertEqual(self.send(self.now + _days(14)), 1)

    def test_nothing_goes_out_outside_office_hours(self):
        self.assertEqual(self.send(self.now.replace(hour=9)), 0)
        self.assertEqual(self.send(self.now.replace(hour=16)), 0)
        self.assertEqual(len(mail.outbox), 0)
