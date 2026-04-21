from ddt import data, ddt
from django.core import mail
from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging import event_logger, models, tasks
from waldur_core.logging.tests.factories import EmailHookFactory, WebHookFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories


class BaseHookApiTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.author = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()

        self.valid_event_types = event_logger.get_valid_events()[:3]
        self.valid_event_groups = event_logger.get_event_groups_keys()[:3]


class HookCreationViewTest(BaseHookApiTest):
    def test_user_can_create_webhook(self):
        self.client.force_authenticate(user=self.author)
        response = self.client.post(
            WebHookFactory.get_list_url(),
            data={
                "event_types": self.valid_event_types,
                "destination_url": "http://example.com/",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_user_can_create_email_hook(self):
        self.client.force_authenticate(user=self.author)
        response = self.client.post(
            reverse("emailhook-list"),
            data={"event_types": self.valid_event_types, "email": "test@example.com"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_user_can_subscribe_to_event_groups(self):
        event_groups = self.valid_event_groups
        event_types = event_logger.expand_event_groups(event_groups)

        self.client.force_authenticate(user=self.author)
        response = self.client.post(
            WebHookFactory.get_list_url(),
            data={
                "event_groups": event_groups,
                "destination_url": "http://example.com/",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["event_groups"], set(event_groups))
        self.assertEqual(response.data["event_types"], set(event_types))


@ddt
class HookUpdateTest(BaseHookApiTest):
    def setUp(self):
        super().setUp()
        self.hooks = {
            "web": WebHookFactory.get_url(WebHookFactory(user=self.author)),
        }

    def test_author_can_update_webhook_destination_url(self):
        new_data = {"destination_url": "http://another-host.com"}
        response = self.update_hook("web", new_data)
        self.assertEqual(new_data["destination_url"], response.data["destination_url"])

    @data(
        "web",
    )
    def test_author_can_update_hook_event_types(self, hook):
        new_event_types = set(self.valid_event_types[:1])
        response = self.update_hook(hook, {"event_types": new_event_types})
        self.assertEqual(new_event_types, response.data["event_types"])

    @data(
        "web",
    )
    def test_author_can_update_event_groups(self, hook):
        event_groups = self.valid_event_groups
        event_types = event_logger.expand_event_groups(event_groups)

        self.client.force_authenticate(user=self.author)
        response = self.update_hook(hook, {"event_groups": event_groups})
        self.assertEqual(response.data["event_groups"], set(event_groups))
        self.assertEqual(response.data["event_types"], set(event_types))

    @data(
        "web",
    )
    def test_author_can_disable_hook(self, hook):
        response = self.update_hook(hook, {"is_active": False})
        self.assertFalse(response.data["is_active"])

    def update_hook(self, hook, data):
        self.client.force_authenticate(user=self.author)
        url = self.hooks[hook]
        response = self.client.patch(url, data=data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response


class HookPermissionsViewTest(BaseHookApiTest):
    def setUp(self):
        super().setUp()
        self.url = WebHookFactory.get_url(WebHookFactory(user=self.author))

    def test_hook_visible_to_author(self):
        self.client.force_authenticate(user=self.author)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(str(self.author.uuid), str(response.data["author_uuid"]))

    def test_hook_visible_to_staff(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_hook_not_visible_to_other_user(self):
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)


class HookFilterViewTest(BaseHookApiTest):
    def test_staff_can_filter_webhook_by_author_uuid(self):
        WebHookFactory(user=self.author)
        WebHookFactory(user=self.other_user)
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            WebHookFactory.get_list_url(), {"author_uuid": self.author.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(str(self.author.uuid), str(response.data[0]["author_uuid"]))

    def test_staff_can_filter_summary_hook_by_author_uuid(self):
        WebHookFactory(user=self.author)
        WebHookFactory(user=self.other_user)
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            reverse("hooks-list"), {"author_uuid": self.author.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(str(self.author.uuid), str(response.data[0]["author_uuid"]))

    def test_summary_lists_both_webhook_and_emailhook(self):
        WebHookFactory(user=self.author)
        EmailHookFactory(user=self.author)
        self.client.force_authenticate(user=self.author)
        response = self.client.get(reverse("hooks-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)
        hook_types = {item["hook_type"] for item in response.data}
        self.assertEqual(hook_types, {"webhook", "email"})

    def test_summary_returns_child_specific_fields(self):
        WebHookFactory(user=self.author, destination_url="http://hook.test/")
        EmailHookFactory(user=self.author, email="notify@example.com")
        self.client.force_authenticate(user=self.author)
        response = self.client.get(reverse("hooks-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data_by_type = {item["hook_type"]: item for item in response.data}
        self.assertEqual(
            data_by_type["webhook"]["destination_url"], "http://hook.test/"
        )
        self.assertEqual(data_by_type["email"]["email"], "notify@example.com")

    def test_summary_filters_by_is_active(self):
        WebHookFactory(user=self.author, is_active=True)
        EmailHookFactory(user=self.author, is_active=False)
        self.client.force_authenticate(user=self.author)
        response = self.client.get(reverse("hooks-list"), {"is_active": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["is_active"])

    def test_non_staff_user_sees_only_own_hooks(self):
        WebHookFactory(user=self.author)
        WebHookFactory(user=self.other_user)
        EmailHookFactory(user=self.other_user)
        self.client.force_authenticate(user=self.author)
        response = self.client.get(reverse("hooks-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)


class SystemNotificationTest(test.APITestCase):
    def setUp(self):
        self.system_notification = factories.SystemNotificationFactory()
        self.event_types = self.system_notification.event_types
        self.project_fixture = structure_fixtures.ProjectFixture()
        self.project = self.project_fixture.project
        self.admin = self.project_fixture.admin
        self.manager = self.project_fixture.manager
        self.event = factories.EventFactory(event_type=self.event_types[0])
        self.feed = models.Feed.objects.create(scope=self.project, event=self.event)

    def test_send_notification_if_user_is_not_subscribed_but_event_type_is_system_type(
        self,
    ):
        self.assertFalse(models.EmailHook.objects.count())
        tasks.process_event(self.event.id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(self.admin.email in mail.outbox[0].to)

    def test_not_send_notification_if_event_type_is_not_system_type(self):
        self.assertFalse(models.EmailHook.objects.count())
        self.event.event_type = "test_event_type"
        self.event.save()
        tasks.process_event(self.event.id)
        self.assertEqual(len(mail.outbox), 0)

    def test_not_send_notification_if_wrong_project(self):
        self.assertFalse(models.EmailHook.objects.count())
        self.feed.delete()
        self.event.save()
        tasks.process_event(self.event.id)
        self.assertEqual(len(mail.outbox), 0)

    def test_not_send_notification_if_wrong_role(self):
        self.assertFalse(models.EmailHook.objects.count())
        self.system_notification.roles = ["manager"]
        self.system_notification.save()
        tasks.process_event(self.event.id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(self.admin.email in mail.outbox[0].to)

    def test_event_groups(self):
        groups = event_logger.get_event_groups()
        group = list(groups.keys())[0]
        self.system_notification.event_groups = [group]
        self.system_notification.event_types = []
        self.system_notification.save()
        self.event.event_type = list(groups[group])[0]
        self.event.save()
        tasks.process_event(self.event.id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(self.admin.email in mail.outbox[0].to)

    @override_waldur_core_settings(NOTIFICATION_SUBJECT="Test Subject")
    def test_notification_subject(self):
        self.assertFalse(models.EmailHook.objects.count())
        tasks.process_event(self.event.id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Test Subject")


class EventGroupsProcessingTest(BaseHookApiTest):
    """Test event group processing with string keys after ORJSON fix"""

    def test_expand_event_groups_with_string_names(self):
        """
        Test that expand_event_groups function works with string group names.
        """
        # Test with string input (as would come from API query params)
        result = event_logger.expand_event_groups(["auth"])

        self.assertIsInstance(result, list)
        self.assertTrue(len(result) > 0)

        # Should contain auth events
        self.assertIn("auth_logged_in_with_username", result)

        # Result should be sorted
        self.assertEqual(result, sorted(result))

    def test_get_event_groups_returns_string_keys(self):
        """
        Test that get_event_groups function returns string keys, not enum objects.
        This is critical for ORJSON serialization compatibility.
        """
        groups = event_logger.get_event_groups()

        self.assertIsInstance(groups, dict)

        # All keys should be strings
        for key in groups.keys():
            self.assertIsInstance(
                key, str, f"Key '{key}' should be a string, not {type(key)}"
            )

        # All values should be lists of strings
        for group_name, event_types in groups.items():
            self.assertIsInstance(event_types, list)
            for event_type in event_types:
                self.assertIsInstance(event_type, str)

    def test_webhook_creation_with_event_groups_string_names(self):
        """
        Test webhook creation using event groups with string names
        (regression test for ORJSON serialization fix).
        """
        self.client.force_authenticate(user=self.author)

        # Create webhook with event groups using string names
        webhook_data = {
            "event_groups": ["auth", "users"],  # String group names
            "destination_url": "http://example.com/webhook/",
        }

        response = self.client.post(WebHookFactory.get_list_url(), webhook_data)
        self.assertEqual(response.status_code, 201)

        # Verify the webhook was created with correct event types
        webhook = models.WebHook.objects.get(uuid=response.data["uuid"])

        # Should have expanded the groups into individual event types
        expected_events = set()
        groups = event_logger.get_event_groups()
        expected_events.update(groups["auth"])
        expected_events.update(groups["users"])

        self.assertEqual(set(webhook.event_types), expected_events)

    def test_email_hook_creation_with_event_groups_string_names(self):
        """
        Test email hook creation using event groups with string names.
        """
        self.client.force_authenticate(user=self.author)

        # Create email hook with event groups using string names
        email_hook_data = {
            "event_groups": ["resources"],  # String group name
            "email": "test@example.com",
        }

        response = self.client.post(reverse("emailhook-list"), email_hook_data)
        self.assertEqual(response.status_code, 201)

        # Verify the email hook was created with correct event types
        email_hook = models.EmailHook.objects.get(uuid=response.data["uuid"])

        # Should have expanded the groups into individual event types
        groups = event_logger.get_event_groups()
        expected_events = set(groups["resources"])

        self.assertEqual(set(email_hook.event_types), expected_events)
