import json
import tempfile
from io import StringIO

from ddt import data, ddt
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status, test

from waldur_core.core.models import Notification, NotificationTemplate
from waldur_core.structure.tests import factories, fixtures


@ddt
class NotificationList(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_1 = factories.NotificationFactory(key="app_name.event_name")
        self.notification_2 = factories.NotificationFactory(key="app_name.event_name2")
        self.url = factories.NotificationFactory.get_list_url()

    @data("staff")
    def test_admin_user_can_list_notifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    @data("user")
    def test_other_can_not_list_notifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class NotificationChangeTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_1 = factories.NotificationFactory(
            key="app_name.event_name", enabled=False
        )
        self.notification_2 = factories.NotificationFactory(
            key="app_name.event_name2", enabled=True
        )
        self.url = factories.NotificationFactory.get_url(self.notification_1)
        self.disable_url = factories.NotificationFactory.get_url(
            self.notification_2, action="disable"
        )
        self.enable_url = factories.NotificationFactory.get_url(
            self.notification_1, action="enable"
        )

    @data("staff")
    def test_staff_can_change_notifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        valid_data = {"key": "appname.template_name"}

        response = self.client.put(self.url, valid_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("user")
    def test_other_can_not_change_customer_organization_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        valid_data = {"key": "appname.template_name"}

        response = self.client.put(self.url, valid_data)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    @data("staff")
    def test_staff_can_disable_notifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.disable_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.notification_2.refresh_from_db()
        self.assertEqual(self.notification_2.enabled, False)

    @data("staff")
    def test_staff_can_enable_notifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.enable_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.notification_1.refresh_from_db()
        self.assertEqual(self.notification_1.enabled, True)

    @data("user")
    def test_other_can_not_disable_notifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.disable_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.notification_2.refresh_from_db()
        self.assertEqual(self.notification_2.enabled, True)

    @data("user")
    def test_other_can_not_enable_notifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.enable_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.notification_1.refresh_from_db()
        self.assertEqual(self.notification_1.enabled, False)


@ddt
class NotificationTemplateListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.template = factories.NotificationTemplateFactory(
            path="marketplace/test.txt"
        )
        self.url = factories.NotificationTemplateFactory.get_list_url()
        self.override_url = factories.NotificationTemplateFactory.get_url(
            self.template, action="override"
        )

    @data("staff", "user")
    def test_everyone_can_list_notification_templates(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)

        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(response.data[0]["path"], self.template.path)

    @data("staff")
    def test_staff_can_override_notification_templates(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        new_content = {"content": "new_content"}

        response = self.client.post(self.override_url, new_content)
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        response = self.client.get(self.url)
        self.template.refresh_from_db()
        self.assertEqual(response.data[0]["content"], self.template.content)

    @data("staff")
    def test_staff_cannot_override_template_with_invalid_content(self, user):
        """
        Verify that updating a template with invalid Django template syntax fails
        in the update (override) view.
        """
        self.client.force_authenticate(getattr(self.fixture, user))

        # Set an initial content override to verify it survives the failed request
        original_content = "This is the original content."
        self.template.content = original_content
        self.template.save(update_fields=["content"])

        # Prepare payload with invalid template syntax (e.g., unmatched tag)
        invalid_content_payload = {
            "content": "{% if invitation['type'] == 'project' %}"
        }

        # Make the API call
        response = self.client.post(self.override_url, invalid_content_payload)

        # Assert the response status and error message
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn("content", response.data)
        self.assertIn("Invalid template syntax", response.data["content"][0])

        # Verify that the template content was not changed
        self.template.refresh_from_db()
        self.assertEqual(self.template.content, original_content)

    @data("staff")
    def test_list_view_handles_invalid_db_content(self, user):
        """
        Verify that the list view still successfully returns content (the raw string)
        even if the content stored in the DB is syntactically invalid.
        """
        self.client.force_authenticate(getattr(self.fixture, user))

        # 1. Store syntactically INVALID content directly on the template model
        # Note: This simulates a template being saved with errors, perhaps via raw SQL
        # or an old process without validation.
        INVALID_CONTENT = (
            "This content has a syntax error: {% if invitation['type'] == 'project' %}"
        )
        self.template.content = INVALID_CONTENT
        self.template.save(update_fields=["content"])

        # 2. Get the list view
        response = self.client.get(self.url)

        # 3. Assert status and content
        # The view should succeed (HTTP 200) because the serializer only fetches
        # the raw string content and does not compile it for listing.
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        template_data = next(
            (item for item in response.data if item["path"] == self.template.path),
            None,
        )

        self.assertIsNotNone(template_data)
        # The list view must return the raw, invalid content string.
        self.assertEqual(template_data["content"], INVALID_CONTENT)

    @data(
        "user",
    )
    def test_other_can_not_override_notification_templates(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        new_content = {"content": "new_content"}
        response = self.client.post(self.override_url, new_content)

        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class NotificationTemplateFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_template_1 = factories.NotificationTemplateFactory(
            name="invitation_approved", path="users/invitation_approved_message.txt"
        )
        self.notification_template_2 = factories.NotificationTemplateFactory(
            name="invitation_rejected", path="users/invitation_rejected_message.txt"
        )
        self.url = factories.NotificationTemplateFactory.get_list_url()

    @data("staff")
    def test_notification_template_name_filter(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), 2)
        response = self.client.get(
            self.url,
            {"name": "invitation"},
        )
        self.assertEqual(len(response.json()), 2)

    @data("staff")
    def test_notification_template_name_exact_filter(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), 2)
        response = self.client.get(
            self.url,
            {"name_exact": "invitation_approved"},
        )
        self.assertEqual(len(response.json()), 1)


class LoadNotificationsCommandTest(TestCase):
    """Tests for the load_notifications management command."""

    def _write_json(self, data):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        return f.name

    def test_empty_config_does_not_override_enabled_notification(self):
        """A notification enabled via API stays enabled when config is empty."""
        notification = factories.NotificationFactory(
            key="users.invitation_created", enabled=True
        )
        call_command("load_notifications", self._write_json({}))
        notification.refresh_from_db()
        self.assertTrue(notification.enabled)

    def test_empty_config_does_not_override_disabled_notification(self):
        """A notification disabled via API stays disabled when config is empty."""
        notification = factories.NotificationFactory(
            key="users.invitation_created", enabled=False
        )
        call_command("load_notifications", self._write_json({}))
        notification.refresh_from_db()
        self.assertFalse(notification.enabled)

    def test_missing_file_does_not_override_enabled_notification(self):
        """A notification enabled via API stays enabled when file is missing."""
        notification = factories.NotificationFactory(
            key="users.invitation_created", enabled=True
        )
        call_command("load_notifications", "/nonexistent/notifications.json")
        notification.refresh_from_db()
        self.assertTrue(notification.enabled)

    def test_explicit_true_enables_notification(self):
        """An explicit true in the config enables a disabled notification."""
        notification = factories.NotificationFactory(
            key="users.invitation_created", enabled=False
        )
        call_command(
            "load_notifications",
            self._write_json({"users.invitation_created": True}),
        )
        notification.refresh_from_db()
        self.assertTrue(notification.enabled)

    def test_explicit_false_disables_notification(self):
        """An explicit false in the config disables an enabled notification."""
        notification = factories.NotificationFactory(
            key="users.invitation_created", enabled=True
        )
        call_command(
            "load_notifications",
            self._write_json({"users.invitation_created": False}),
        )
        notification.refresh_from_db()
        self.assertFalse(notification.enabled)

    def test_new_notification_created_as_disabled_by_default(self):
        """Notifications not in the DB are created with enabled=False."""
        Notification.objects.filter(key="users.invitation_created").delete()
        call_command("load_notifications", self._write_json({}))
        notification = Notification.objects.get(key="users.invitation_created")
        self.assertFalse(notification.enabled)


class LoadNotificationsPruneTest(TestCase):
    """Tests for orphaned-notification reporting and pruning."""

    def _write_json(self, data):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        return f.name

    def test_orphaned_notification_is_reported_but_kept_without_prune(self):
        """A row whose key is not registered is reported, not deleted, by default."""
        notification = factories.NotificationFactory(key="fake_app.orphaned_event")
        out = StringIO()
        call_command("load_notifications", self._write_json({}), stdout=out)
        self.assertTrue(Notification.objects.filter(pk=notification.pk).exists())
        self.assertIn("Orphaned notification 'fake_app.orphaned_event'", out.getvalue())
        self.assertIn("Re-run with --prune", out.getvalue())

    def test_prune_removes_orphaned_notification(self):
        """--prune deletes a Notification row whose key is not registered."""
        notification = factories.NotificationFactory(key="fake_app.orphaned_event")
        call_command("load_notifications", self._write_json({}), "--prune")
        self.assertFalse(Notification.objects.filter(pk=notification.pk).exists())

    def test_prune_removes_unreferenced_templates(self):
        """--prune deletes templates that no registered notification declares."""
        notification = factories.NotificationFactory(key="fake_app.orphaned_event")
        template_ids = list(notification.templates.values_list("pk", flat=True))
        self.assertTrue(template_ids)
        call_command("load_notifications", self._write_json({}), "--prune")
        self.assertFalse(
            NotificationTemplate.objects.filter(pk__in=template_ids).exists()
        )

    def test_prune_keeps_template_shared_with_registered_notification(self):
        """A template still declared by a registered notification survives pruning."""
        call_command("load_notifications", self._write_json({}))
        shared_template = NotificationTemplate.objects.filter(
            notification__isnull=False
        ).first()
        self.assertIsNotNone(shared_template)

        orphan = factories.NotificationFactory(key="fake_app.orphaned_event")
        orphan.templates.add(shared_template)

        call_command("load_notifications", self._write_json({}), "--prune")

        self.assertFalse(Notification.objects.filter(pk=orphan.pk).exists())
        self.assertTrue(
            NotificationTemplate.objects.filter(pk=shared_template.pk).exists()
        )

    def test_prune_keeps_customised_template(self):
        """A template with operator-overridden content survives pruning."""
        notification = factories.NotificationFactory(key="fake_app.orphaned_event")
        template = notification.templates.first()
        template.content = "Custom override content"
        template.save()

        call_command("load_notifications", self._write_json({}), "--prune")

        self.assertFalse(Notification.objects.filter(pk=notification.pk).exists())
        self.assertTrue(NotificationTemplate.objects.filter(pk=template.pk).exists())
