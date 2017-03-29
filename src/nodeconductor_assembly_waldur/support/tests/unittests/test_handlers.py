from django.core import mail

from .. import factories, base


class IssueUpdatedHandlerTest(base.BaseTest):

    def test_email_notification_is_sent_when_issue_is_updated(self):
        issue = factories.IssueFactory()

        issue.summary = 'new summary'
        issue.save()

        self.assertEqual(len(mail.outbox), 1)

    def test_email_notification_is_not_sent_on_issue_creation(self):
        factories.IssueFactory()

        self.assertEqual(len(mail.outbox), 0)


class CommentCreatedHandlerTest(base.BaseTest):

    def test_email_is_sent_when_comment_is_created(self):
        factories.CommentFactory()

        self.assertEqual(len(mail.outbox), 1)

    def test_email_is_not_sent_when_comment_is_updated(self):
        comment = factories.CommentFactory()
        self.assertEqual(len(mail.outbox), 1)

        comment.description = 'new_description'
        comment.save()

        self.assertEqual(len(mail.outbox), 1)

