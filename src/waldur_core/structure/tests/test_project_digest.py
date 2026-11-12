from unittest import mock

from django.core import mail
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Notification
from waldur_core.logging.models import EmailLog
from waldur_core.structure import digest_tasks
from waldur_core.structure.digest_providers import DigestSectionData
from waldur_core.structure.models import ProjectDigestConfiguration
from waldur_core.structure.tests import fixtures

NOTIFICATION_KEY = "structure.project_digest"

TEAM_SECTION = DigestSectionData(
    key="team_changes",
    title_key="Team changes",
    order=40,
    context={
        "joined_by_role": {"Member": 1},
        "left_by_role": {},
        "total_joined": 1,
        "total_left": 0,
    },
    text_template="structure/digest_team_summary.txt",
    html_template="structure/digest_team_summary.html",
)


def _set_notification(enabled):
    Notification.objects.update_or_create(
        key=NOTIFICATION_KEY, defaults={"enabled": enabled}
    )


@override_config(ENABLE_PROJECT_DIGEST=True)
class ProjectDigestSendTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.member = self.fixture.member
        self.config = ProjectDigestConfiguration.objects.create(
            customer=self.fixture.customer, is_enabled=True
        )

    @mock.patch.object(
        digest_tasks, "_gather_project_sections", return_value=[TEAM_SECTION]
    )
    def test_digest_is_sent_and_logged_when_notification_is_enabled(self, _):
        _set_notification(True)

        digest_tasks.send_digest_for_customer(self.config.pk)

        recipients = [message.to[0] for message in mail.outbox]
        self.assertIn(self.member.email, recipients)
        self.assertTrue(
            EmailLog.objects.filter(emails__contains=[self.member.email]).exists()
        )

    @mock.patch.object(
        digest_tasks, "_gather_project_sections", return_value=[TEAM_SECTION]
    )
    def test_digest_is_not_sent_when_notification_is_disabled(self, gather):
        _set_notification(False)

        digest_tasks.send_digest_for_customer(self.config.pk)

        self.assertEqual(len(mail.outbox), 0)
        gather.assert_not_called()
        self.config.refresh_from_db()
        self.assertIsNone(self.config.last_sent_at)

    @freeze_time("2026-11-05")
    @mock.patch.object(digest_tasks.send_digest_for_customer, "delay")
    def test_daily_task_dispatches_due_digest_when_notification_is_enabled(self, delay):
        _set_notification(True)
        self.config.day_of_month = 5
        self.config.save()

        self.assertEqual(digest_tasks.send_project_digest_notifications(), 1)
        delay.assert_called_once_with(self.config.pk)

    @freeze_time("2026-11-05")
    @mock.patch.object(digest_tasks.send_digest_for_customer, "delay")
    def test_daily_task_dispatches_nothing_when_notification_is_disabled(self, delay):
        _set_notification(False)
        self.config.day_of_month = 5
        self.config.save()

        self.assertEqual(digest_tasks.send_project_digest_notifications(), 0)
        delay.assert_not_called()


@override_config(ENABLE_PROJECT_DIGEST=True)
class ProjectDigestSendTestEmailTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff = self.fixture.staff
        self.client.force_authenticate(user=self.staff)
        self.url = (
            reverse("customer-detail", kwargs={"uuid": self.fixture.customer.uuid.hex})
            + "project-digest-config/send-test/"
        )
        _set_notification(True)

    def assert_not_sent(self, response, reason):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["sent"])
        self.assertIn("No test digest email was sent.", response.data["detail"])
        self.assertIn(reason, response.data["detail"])
        self.assertEqual(len(mail.outbox), 0)

    @mock.patch.object(
        digest_tasks, "_gather_project_sections", return_value=[TEAM_SECTION]
    )
    def test_reports_that_the_test_email_was_sent(self, _):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["sent"])
        self.assertEqual(response.data["detail"], "Test digest email has been sent.")
        self.assertEqual(len(mail.outbox), 1)

    @mock.patch.object(digest_tasks, "_gather_project_sections", return_value=[])
    def test_reports_that_nothing_was_sent_when_no_project_has_data(self, _):
        response = self.client.post(self.url)

        self.assert_not_sent(response, "No project has anything to report")

    def test_reports_that_nothing_was_sent_when_notification_is_disabled(self):
        _set_notification(False)

        response = self.client.post(self.url)

        self.assert_not_sent(response, "Ask your administrator to enable it.")

    def test_reports_that_nothing_was_sent_without_an_email_address(self):
        self.staff.email = ""
        self.staff.save()

        response = self.client.post(self.url)

        self.assert_not_sent(response, "Your account has no email address.")

    @mock.patch(
        "waldur_core.structure.digest_providers.get_available_providers",
        return_value=[],
    )
    def test_reports_that_nothing_was_sent_without_digest_sections(self, _):
        response = self.client.post(self.url)

        self.assert_not_sent(response, "No digest sections are available")

    @mock.patch.object(
        digest_tasks, "broadcast_mail", side_effect=ConnectionRefusedError
    )
    @mock.patch.object(
        digest_tasks, "_gather_project_sections", return_value=[TEAM_SECTION]
    )
    def test_reports_that_nothing_was_sent_when_mail_server_is_down(self, *_):
        response = self.client.post(self.url)

        self.assert_not_sent(response, "Sending it failed")
