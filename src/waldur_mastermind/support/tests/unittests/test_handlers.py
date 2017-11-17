from django.conf import settings
from django.core import mail
from django.test import TransactionTestCase

from .. import factories


class BaseHandlerTest(TransactionTestCase):

    def setUp(self):
        settings.CELERY_ALWAYS_EAGER = True
        settings.WALDUR_SUPPORT['ENABLED'] = True

    def tearDown(self):
        settings.CELERY_ALWAYS_EAGER = False
        settings.WALDUR_SUPPORT['ENABLED'] = False


class IssueUpdatedHandlerTest(BaseHandlerTest):

    def test_email_notification_is_sent_when_issue_is_updated(self):
        issue = factories.IssueFactory()

        issue.summary = 'new_summary'
        issue.save()

        self.assertEqual(len(mail.outbox), 1)

    def test_email_notification_is_not_sent_on_issue_creation(self):
        factories.IssueFactory()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_notification_is_not_sent_if_feature_is_suppressed(self):
        with self.settings(SUPPRESS_NOTIFICATION_EMAILS=True):
            issue = factories.IssueFactory()

            issue.summary = 'new_summary'
            issue.save()

            self.assertEqual(len(mail.outbox), 0)

    def test_email_notification_is_not_sent_if_assignee_changes(self):
        issue = factories.IssueFactory()

        issue.assignee = factories.SupportUserFactory()
        issue.save()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_notification_is_sent_if_assignee_was_changed_with_status(self):
        issue = factories.IssueFactory()

        issue.assignee = factories.SupportUserFactory()
        issue.status = 'new_status'
        issue.save()

        self.assertEqual(len(mail.outbox), 1)


class CommentCreatedHandlerTest(BaseHandlerTest):

    def test_email_is_sent_when_public_comment_is_created(self):
        factories.CommentFactory(is_public=True)

        self.assertEqual(len(mail.outbox), 1)

    def test_email_is_not_sent_for_private_comment(self):
        factories.CommentFactory()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_is_not_sent_when_public_comment_is_updated(self):
        comment = factories.CommentFactory(is_public=True)
        self.assertEqual(len(mail.outbox), 1)

        comment.description = 'new_description'
        comment.save()

        self.assertEqual(len(mail.outbox), 1)

    def test_email_is_not_sent_for_own_comments(self):
        issue = factories.IssueFactory()
        factories.CommentFactory(issue=issue, is_public=True, author__user=issue.caller)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_is_not_sent_if_feature_is_suppressed(self):
        with self.settings(SUPPRESS_NOTIFICATION_EMAILS=True):
            factories.CommentFactory(is_public=True)

            self.assertEqual(len(mail.outbox), 0)
