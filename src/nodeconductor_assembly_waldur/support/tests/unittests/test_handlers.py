from django.conf import settings
from django.core import mail
from django.test import TestCase

from .. import factories


class BaseHandlerTest(TestCase):

    def setUp(self):
        settings.CELERY_ALWAYS_EAGER = True

    def tearDown(self):
        settings.CELERY_ALWAYS_EAGER = False


class IssueUpdatedHandlerTest(BaseHandlerTest):

    def test_email_notification_is_sent_when_issue_is_updated(self):
        issue = factories.IssueFactory()

        issue.summary = 'new summary'
        issue.save()

        self.assertEquals(len(mail.outbox), 1)

    def test_email_notification_is_not_sent_on_issue_creation(self):
        factories.IssueFactory()

        self.assertEquals(len(mail.outbox), 0)


class CommentCreatedHandlerTest(TestCase):

    def test_email_is_sent_when_comment_is_created(self):
        factories.CommentFactory()

        self.assertEquals(len(mail.outbox), 1)

    def test_email_is_not_sent_when_comment_is_updated(self):
        comment = factories.CommentFactory()
        self.assertEquals(len(mail.outbox), 1)

        comment.description = 'new_description'
        comment.save()

        self.assertEquals(len(mail.outbox), 1)

