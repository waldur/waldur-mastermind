from constance.test.unittest import override_config
from django.core import mail
from django.test import TestCase, override_settings

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.backend.basic import BasicBackend
from waldur_mastermind.support.tests import factories

# `base.BaseTest` mocks `get_active_backend` wholesale, so these cases drive the
# real backend instead. Auto-assign is pinned off so it cannot perturb the saves
# the handler keys on.
BASIC = override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)

NOTIFICATION_KEY = "support.notification_issue_updated"


@BASIC
@override_settings(task_always_eager=True)
class IssueUpdatedNotificationHandlerTest(TestCase):
    """The caller hears about a change, but not about the ticket appearing."""

    def setUp(self):
        structure_factories.NotificationFactory(key=NOTIFICATION_KEY, enabled=True)
        self.caller = structure_factories.UserFactory(email="caller@example.com")
        self.backend = BasicBackend()

    def blank_issue(self, **kwargs):
        """An issue as the API leaves it before the backend materialises it."""
        issue = factories.IssueFactory(caller=self.caller, **kwargs)
        models.Issue.objects.filter(pk=issue.pk).update(
            backend_id="", key="", status=""
        )
        issue.refresh_from_db()
        return issue

    def create_issue(self, **kwargs):
        """Create an issue the way the API does — a blank save, then the backend."""
        issue = self.blank_issue(**kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            self.backend.create_issue(issue)
        return issue

    def test_creating_a_ticket_does_not_mail_the_caller(self):
        # The backend's save sets the default status, which used to read as an
        # update of a ticket that had existed for a fraction of a second.
        self.create_issue()

        self.assertEqual(len(mail.outbox), 0)

    def test_a_later_summary_change_still_mails_the_caller(self):
        issue = self.create_issue()

        with self.captureOnCommitCallbacks(execute=True):
            issue.summary = "Edited after the fact"
            issue.save()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["caller@example.com"])

    def test_a_later_status_change_still_mails_the_caller(self):
        issue = self.create_issue()
        models.IssueStatusTransition.objects.create(
            from_status="Open", to_status="Resolved"
        )

        with self.captureOnCommitCallbacks(execute=True):
            issue.status = "Resolved"
            self.backend.update_issue(issue)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["caller@example.com"])

    @override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="atlassian")
    def test_an_external_backend_materialises_its_ticket_just_as_quietly(self):
        # Every backend creates an issue in two saves — the second one carries
        # the backend id — so the gate must not be specific to the basic one.
        issue = self.blank_issue()

        with self.captureOnCommitCallbacks(execute=True):
            issue.backend_id = "JIRA-1"
            issue.key = "JIRA-1"
            issue.status = "Open"
            issue.save()

        self.assertEqual(len(mail.outbox), 0)

        with self.captureOnCommitCallbacks(execute=True):
            issue.status = "Resolved"
            issue.save()

        self.assertEqual(len(mail.outbox), 1)
