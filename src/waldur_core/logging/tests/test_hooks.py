from ddt import data, ddt
from django.core import mail
from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging import loggers, models, tasks
from waldur_core.logging.tests.factories import WebHookFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories


class BaseHookApiTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.author = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()

        self.valid_event_types = loggers.get_valid_events()[:3]
        self.valid_event_groups = loggers.get_event_groups_keys()[:3]


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
        event_types = loggers.expand_event_groups(event_groups)

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
        event_types = loggers.expand_event_groups(event_groups)

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


class SystemNotificationTest(test.APITransactionTestCase):
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
        groups = loggers.get_event_groups()
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
