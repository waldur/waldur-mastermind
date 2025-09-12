import unittest

from constance import config
from constance.test.unittest import override_config
from django.core import mail
from django.template import Context, Template
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import handlers
from waldur_mastermind.support.tests import factories


@override_config(WALDUR_SUPPORT_ENABLED=True)
@override_settings(task_always_eager=True)
class IssueUpdatedHandlerTest(TransactionTestCase):
    def setUp(self):
        self.notification1 = structure_factories.NotificationFactory(
            key="support.notification_comment_added", enabled=True
        )
        self.notification2 = structure_factories.NotificationFactory(
            key="support.notification_issue_feedback", enabled=True
        )
        self.notification3 = structure_factories.NotificationFactory(
            key="support.notification_comment_updated", enabled=True
        )
        self.notification4 = structure_factories.NotificationFactory(
            key="support.notification_issue_updated", enabled=True
        )

    def test_email_notification_is_sent_when_issue_is_updated(self):
        issue = factories.IssueFactory()

        issue.summary = "new_summary"
        issue.save()

        self.assertEqual(len(mail.outbox), 1)

    @unittest.skip
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

    @unittest.skip
    def test_email_notification_is_not_sent_if_feature_is_suppressed(self):
        self.notification1.enabled = False
        self.notification2.enabled = False
        self.notification3.enabled = False
        self.notification4.enabled = False
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
    def setUp(self):
        self.notification1 = structure_factories.NotificationFactory(
            key="support.notification_comment_added", enabled=True
        )
        self.notification2 = structure_factories.NotificationFactory(
            key="support.notification_issue_feedback", enabled=True
        )
        self.notification3 = structure_factories.NotificationFactory(
            key="support.notification_comment_updated", enabled=True
        )
        self.notification4 = structure_factories.NotificationFactory(
            key="support.notification_issue_updated", enabled=True
        )

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

    @unittest.skip
    def test_email_is_not_sent_if_feature_is_suppressed(self):
        self.notification1.enabled = False
        self.notification2.enabled = False
        self.notification3.enabled = False
        self.notification4.enabled = False
        factories.CommentFactory(is_public=True)

        self.assertEqual(len(mail.outbox), 0)


class CustomerDeletionHandlerTest(TransactionTestCase):
    def test_customer_deletion_with_issue_attachments_does_not_fail(self):
        """Test that deleting a customer with related issues and attachments doesn't raise AttributeError."""
        # Create a customer with a project
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)

        # Create an issue related to the project
        issue = factories.IssueFactory(customer=customer, project=project)

        # Create an attachment for the issue
        factories.AttachmentFactory(issue=issue)

        # Attempt to delete the customer - this should not raise an error
        try:
            customer.delete()
        except AttributeError as e:
            if "'NoneType' object has no attribute '_base_manager'" in str(e):
                self.fail(
                    "AttributeError raised when deleting customer with issue attachments"
                )
            else:
                raise

    def test_get_issue_scopes_handles_deleted_resource(self):
        """Test that get_issue_scopes handles cases where resource is already deleted."""
        # Create an issue with a resource
        issue = factories.IssueFactory()

        # Manually set resource content type and ID to simulate a deleted resource
        from django.contrib.contenttypes.models import ContentType

        issue.resource_content_type = ContentType.objects.get_for_model(
            structure_factories.ProjectFactory._meta.model
        )
        issue.resource_object_id = 999999  # Non-existent ID
        issue.save()

        # This should not raise an error
        scopes = handlers.get_issue_scopes(issue)

        # Should still return customer if it exists
        if issue.customer:
            self.assertIn(issue.customer, scopes)
