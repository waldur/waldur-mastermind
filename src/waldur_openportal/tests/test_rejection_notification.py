from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import ReviewStates
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import models, tasks


class RejectionNotificationViewTest(APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.project_template = models.ProjectTemplate.objects.create(
            name="test-template",
            portal="test.example.com",
        )
        self.managed_project = models.ManagedProject.objects.create(
            destination="test.example.com",
            identifier="proj-001",
            state=ReviewStates.PENDING,
            project_template=self.project_template,
        )
        self.url = reverse(
            "openportal-managed-project-reject",
            kwargs={
                "identifier": self.managed_project.identifier,
                "destination": self.managed_project.destination,
            },
        )

    @patch("waldur_openportal.tasks.notify_users_about_rejected_allocation.delay")
    def test_reject_dispatches_notification_task(self, mock_delay):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {"comment": "Quota exceeded."})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_delay.assert_called_once_with(
            core_utils.serialize_instance(self.managed_project)
        )

    @patch("waldur_openportal.tasks.notify_users_about_rejected_allocation.delay")
    def test_reject_does_not_dispatch_when_not_permitted(self, mock_delay):
        unprivileged = structure_factories.UserFactory()
        self.client.force_authenticate(unprivileged)
        response = self.client.post(self.url, {"comment": "..."})
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        mock_delay.assert_not_called()


class RejectionNotificationTaskTest(TestCase):
    def setUp(self):
        self.reviewer = structure_factories.UserFactory(
            first_name="Alice",
            last_name="Smith",
            email="alice@example.com",
            organization="University of Tartu",
        )
        self.admin = structure_factories.UserFactory(
            first_name="Bob",
            email="admin@example.com",
        )
        self.manager = structure_factories.UserFactory(
            first_name="Carol",
            email="manager@example.com",
        )
        self.member = structure_factories.UserFactory(
            first_name="Dave",
            email="member@example.com",
        )
        self.project = structure_factories.ProjectFactory()
        self.project.add_user(self.admin, ProjectRole.ADMIN)
        self.project.add_user(self.manager, ProjectRole.MANAGER)
        self.project.add_user(self.member, ProjectRole.MEMBER)

        self.project_template = models.ProjectTemplate.objects.create(
            name="test-template",
            portal="test.example.com",
        )
        self.managed_project = models.ManagedProject.objects.create(
            destination="test.example.com",
            identifier="proj-001",
            state=ReviewStates.REJECTED,
            reviewed_by=self.reviewer,
            review_comment="Quota exceeded.",
            project_template=self.project_template,
            project=self.project,
            details={"name": "My Test Project"},
        )
        structure_factories.NotificationFactory(
            key="openportal.managed_project_rejected"
        )

    def _serialize(self):
        return core_utils.serialize_instance(self.managed_project)

    def test_sends_email_to_admins_and_managers(self):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        recipients = {m.to[0] for m in mail.outbox}
        self.assertEqual(recipients, {"admin@example.com", "manager@example.com"})

    def test_does_not_email_plain_members(self):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        recipients = {m.to[0] for m in mail.outbox}
        self.assertNotIn("member@example.com", recipients)

    def test_user_with_both_roles_receives_single_email(self):
        self.project.add_user(self.admin, ProjectRole.MANAGER)
        tasks.notify_users_about_rejected_allocation(self._serialize())
        admin_emails = [m for m in mail.outbox if m.to[0] == "admin@example.com"]
        self.assertEqual(len(admin_emails), 1)

    def test_email_subject_mentions_resource_allocation(self):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        self.assertTrue(len(mail.outbox) > 0)
        self.assertIn("resource allocation", mail.outbox[0].subject.lower())

    def test_email_body_contains_project_name(self):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        self.assertTrue(len(mail.outbox) > 0)
        self.assertIn("My Test Project", mail.outbox[0].body)

    def test_email_body_contains_reviewer_email(self):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        self.assertTrue(len(mail.outbox) > 0)
        self.assertIn("alice@example.com", mail.outbox[0].body)

    def test_recipients_addressed_by_first_name(self):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        admin_email = next(m for m in mail.outbox if m.to[0] == "admin@example.com")
        manager_email = next(m for m in mail.outbox if m.to[0] == "manager@example.com")
        self.assertIn("Bob", admin_email.body)
        self.assertIn("Carol", manager_email.body)

    @patch("waldur_openportal.tasks.core_utils.broadcast_mail")
    def test_uses_correct_notification_key(self, mock_broadcast_mail):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        for call in mock_broadcast_mail.call_args_list:
            self.assertEqual(call[0][0], "openportal")
            self.assertEqual(call[0][1], "managed_project_rejected")

    @patch("waldur_openportal.tasks.core_utils.broadcast_mail")
    def test_context_contains_required_fields(self, mock_broadcast_mail):
        tasks.notify_users_about_rejected_allocation(self._serialize())
        self.assertTrue(mock_broadcast_mail.called)
        context = mock_broadcast_mail.call_args_list[0][0][2]
        self.assertEqual(context["project_name"], "My Test Project")
        self.assertEqual(context["reviewer_full_name"], "Alice Smith")
        self.assertEqual(context["reviewer_email"], "alice@example.com")
        self.assertEqual(context["reviewer_organization"], "University of Tartu")
        self.assertEqual(context["review_comment"], "Quota exceeded.")
        self.assertIn("site_name", context)

    @patch("waldur_openportal.tasks.core_utils.broadcast_mail")
    def test_no_email_sent_when_project_not_linked(self, mock_broadcast_mail):
        models.ManagedProject.objects.filter(pk=self.managed_project.pk).update(
            project=None
        )
        self.managed_project.refresh_from_db()
        tasks.notify_users_about_rejected_allocation(self._serialize())
        mock_broadcast_mail.assert_not_called()

    @patch("waldur_openportal.tasks.core_utils.broadcast_mail")
    def test_no_email_sent_when_reviewer_missing(self, mock_broadcast_mail):
        models.ManagedProject.objects.filter(pk=self.managed_project.pk).update(
            reviewed_by=None
        )
        self.managed_project.refresh_from_db()
        tasks.notify_users_about_rejected_allocation(self._serialize())
        mock_broadcast_mail.assert_not_called()

    @patch("waldur_openportal.tasks.core_utils.broadcast_mail")
    def test_no_email_sent_when_no_admins_or_managers(self, mock_broadcast_mail):
        empty_project = structure_factories.ProjectFactory()
        models.ManagedProject.objects.filter(pk=self.managed_project.pk).update(
            project=empty_project
        )
        self.managed_project.refresh_from_db()
        tasks.notify_users_about_rejected_allocation(self._serialize())
        mock_broadcast_mail.assert_not_called()

    @patch("waldur_openportal.tasks.core_utils.broadcast_mail")
    def test_raises_when_project_not_rejected(self, mock_broadcast_mail):
        models.ManagedProject.objects.filter(pk=self.managed_project.pk).update(
            state=ReviewStates.PENDING
        )
        self.managed_project.refresh_from_db()
        with self.assertRaises(ValueError):
            tasks.notify_users_about_rejected_allocation(self._serialize())
        mock_broadcast_mail.assert_not_called()
