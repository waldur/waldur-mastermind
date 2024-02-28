from constance import config
from constance.test.pytest import override_config
from django.core import mail
from django.template import Context, Template
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from waldur_mastermind.support.tests import factories


@override_config(WALDUR_SUPPORT_ENABLED=True)
@override_settings(task_always_eager=True)
class IssueUpdatedHandlerTest(TransactionTestCase):
    def test_email_notification_is_sent_when_issue_is_updated(self):
        issue = factories.IssueFactory()

        issue.summary = "new_summary"
        issue.save()

        self.assertEqual(len(mail.outbox), 1)

    def test_old_and_new_summary_is_rendered_in_email(self):
        issue = factories.IssueFactory(summary="old summary")

        issue.summary = "new summary"
        issue.save()

        body = mail.outbox[0].body
        self.assertTrue("old summary" in body)
        self.assertTrue("new summary" in body)

    def test_common_footer_is_rendered_in_email(self):
        issue = factories.IssueFactory()
        config.COMMON_FOOTER_TEXT = "Waldur Team!"
        config.COMMON_FOOTER_HTML = "<p>Waldur Team!</p>"

        issue.summary = "new summary"
        issue.save()

        body = mail.outbox[0].body
        self.assertIn(config.COMMON_FOOTER_TEXT, body)

    def test_email_notification_is_not_sent_on_issue_creation(self):
        factories.IssueFactory()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_notification_is_not_sent_if_feature_is_suppressed(self):
        with self.settings(SUPPRESS_NOTIFICATION_EMAILS=True):
            issue = factories.IssueFactory()

            issue.summary = "new_summary"
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
        issue.status = "new_status"
        issue.save()

        self.assertEqual(len(mail.outbox), 1)

    def test_email_notification_is_not_sent_if_issue_just_has_not_been_created_on_backend_yet(
        self,
    ):
        issue = factories.IssueFactory(backend_id="")
        issue.status = "new_status"
        issue.save()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_notification_is_not_sent_if_issue_just_has_been_created_on_backend(
        self,
    ):
        issue = factories.IssueFactory(backend_id="")
        issue.backend_id = "new_backend_id"
        issue.save()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_notification_is_not_sent_if_issue_status_is_ignored(self):
        issue = factories.IssueFactory()

        issue.status = factories.IgnoredIssueStatusFactory().name
        issue.save()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_notification_subject_include_issue_summary(self):
        issue = factories.IssueFactory()

        new_summary = "new_summary"
        issue.summary = new_summary
        issue.save()

        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(new_summary in mail.outbox[0].subject)

    def test_subject_does_not_use_autoescape(self):
        issue = factories.IssueFactory()

        new_summary = "Request for 'Custom VPC'"
        issue.summary = new_summary
        issue.save()

        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(new_summary in mail.outbox[0].subject)

    def test_email_notification_body_if_custom_template_not_exists(self):
        issue = factories.IssueFactory()
        factories.TemplateStatusNotificationFactory()

        new_summary = "new_summary"
        body = "Test template %s" % new_summary
        issue.summary = new_summary
        issue.save()

        self.assertEqual(len(mail.outbox), 1)
        self.assertNotEqual(body, mail.outbox[0].body)

    def test_email_notification_body_if_custom_template_exists(self):
        issue = factories.IssueFactory()
        template = factories.TemplateStatusNotificationFactory()

        new_summary = "new_summary"
        body = "Test template %s" % new_summary
        issue.summary = new_summary
        issue.status = template.status
        issue.save()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(body, mail.outbox[0].body)

    def test_email_notification_if_issue_is_resolved(self):
        issue = factories.IssueFactory()
        template_text = (
            "{{issue.summary}} "
            "{{issue.key}} "
            "you ticket has been resolved at "
            '{{issue.resolution_date|date:"G"}} hours {{issue.resolution_date|date:"i"}} minutes.'
        )
        template = factories.TemplateStatusNotificationFactory(
            status="Resolved", text=template_text
        )

        new_summary = "new_summary"
        issue.summary = new_summary
        issue.status = template.status
        issue.resolution_date = timezone.now()
        issue.save()

        body = Template(template_text).render(Context({"issue": issue}))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(body, mail.outbox[0].body)


@override_config(WALDUR_SUPPORT_ENABLED=True)
@override_settings(task_always_eager=True)
class CommentCreatedHandlerTest(TransactionTestCase):
    def test_email_is_sent_when_public_comment_is_created(self):
        factories.CommentFactory(is_public=True)

        self.assertEqual(len(mail.outbox), 1)

    def test_email_is_not_sent_for_private_comment(self):
        factories.CommentFactory()

        self.assertEqual(len(mail.outbox), 0)

    def test_email_is_not_sent_when_public_comment_is_updated(self):
        comment = factories.CommentFactory(is_public=True)
        self.assertEqual(len(mail.outbox), 1)

        comment.description = "new_description"
        comment.save()

        # First mail is for creation, second mail is for update
        self.assertEqual(len(mail.outbox), 2)

    def test_email_is_not_sent_for_own_comments(self):
        issue = factories.IssueFactory()
        factories.CommentFactory(issue=issue, is_public=True, author__user=issue.caller)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_is_not_sent_if_feature_is_suppressed(self):
        with self.settings(SUPPRESS_NOTIFICATION_EMAILS=True):
            factories.CommentFactory(is_public=True)

            self.assertEqual(len(mail.outbox), 0)
