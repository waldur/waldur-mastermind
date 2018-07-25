from ddt import ddt, data
from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging import loggers
from waldur_core.logging.tests.factories import WebHookFactory, PushHookFactory
from waldur_core.structure.tests import factories as structure_factories


class BaseHookApiTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.author = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()

        self.valid_event_types = loggers.get_valid_events()[:3]
        self.valid_event_groups = loggers.get_event_groups_keys()


class HookCreationViewTest(BaseHookApiTest):

    def test_user_can_create_webhook(self):
        self.client.force_authenticate(user=self.author)
        response = self.client.post(WebHookFactory.get_list_url(), data={
            'event_types': self.valid_event_types,
            'destination_url': 'http://example.com/'
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_user_can_create_email_hook(self):
        self.client.force_authenticate(user=self.author)
        response = self.client.post(reverse('emailhook-list'), data={
            'event_types': self.valid_event_types,
            'email': 'test@example.com'
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_user_can_create_push_hook(self):
        self.client.force_authenticate(user=self.author)
        response = self.client.post(PushHookFactory.get_list_url(), data={
            'event_types': self.valid_event_types,
            'token': 'VALID_TOKEN',
            'type': 'Android'
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_user_can_subscribe_to_event_groups(self):
        event_groups = self.valid_event_groups
        event_types = loggers.expand_event_groups(event_groups)

        self.client.force_authenticate(user=self.author)
        response = self.client.post(WebHookFactory.get_list_url(), data={
            'event_groups': event_groups,
            'destination_url': 'http://example.com/'
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['event_groups'], set(event_groups))
        self.assertEqual(response.data['event_types'], set(event_types))


@ddt
class HookUpdateTest(BaseHookApiTest):
    def setUp(self):
        super(HookUpdateTest, self).setUp()
        self.hooks = {
            'web': WebHookFactory.get_url(WebHookFactory(user=self.author)),
            'push': PushHookFactory.get_url(PushHookFactory(user=self.author))
        }

    def test_author_can_update_webhook_destination_url(self):
        new_data = {
            'destination_url': 'http://another-host.com'
        }
        response = self.update_hook('web', new_data)
        self.assertEqual(new_data['destination_url'], response.data['destination_url'])

    def test_author_can_update_push_hook_token(self):
        new_data = {
            'token': 'NEW_VALID_TOKEN'
        }
        response = self.update_hook('push', new_data)
        self.assertEqual(new_data['token'], response.data['token'])

    @data('web', 'push')
    def test_author_can_update_hook_event_types(self, hook):
        new_event_types = set(self.valid_event_types[:1])
        response = self.update_hook(hook, {'event_types': new_event_types})
        self.assertEqual(new_event_types, response.data['event_types'])

    @data('web', 'push')
    def test_author_can_update_event_groups(self, hook):
        event_groups = self.valid_event_groups
        event_types = loggers.expand_event_groups(event_groups)

        self.client.force_authenticate(user=self.author)
        response = self.update_hook(hook, {
            'event_groups': event_groups
        })
        self.assertEqual(response.data['event_groups'], set(event_groups))
        self.assertEqual(response.data['event_types'], set(event_types))

    @data('web', 'push')
    def test_author_can_disable_hook(self, hook):
        response = self.update_hook(hook, {'is_active': False})
        self.assertFalse(response.data['is_active'])

    def update_hook(self, hook, data):
        self.client.force_authenticate(user=self.author)
        url = self.hooks[hook]
        response = self.client.patch(url, data=data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response


class HookPermissionsViewTest(BaseHookApiTest):

    def setUp(self):
        super(HookPermissionsViewTest, self).setUp()
        self.url = WebHookFactory.get_url(WebHookFactory(user=self.author))

    def test_hook_visible_to_author(self):
        self.client.force_authenticate(user=self.author)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.author.uuid, response.data['author_uuid'])

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
        response = self.client.get(WebHookFactory.get_list_url(), {'author_uuid': self.author.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(self.author.uuid, response.data[0]['author_uuid'])

    def test_staff_can_filter_summary_hook_by_author_uuid(self):
        WebHookFactory(user=self.author)
        PushHookFactory(user=self.other_user)
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(reverse('hooks-list'), {'author_uuid': self.author.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(self.author.uuid, response.data[0]['author_uuid'])
