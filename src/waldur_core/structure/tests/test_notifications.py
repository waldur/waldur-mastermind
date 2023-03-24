from dbtemplates.models import Template
from ddt import data, ddt
from django.template.loader import get_template
from rest_framework import status, test

from waldur_core.structure.tests import factories, fixtures


@ddt
class NotificationList(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_1 = factories.NotificationFactory(key='app_name.event_name')
        self.notification_2 = factories.NotificationFactory(key='app_name.event_name2')
        self.url = factories.NotificationFactory.get_list_url()

    @data('staff')
    def test_admin_user_can_list_notifications(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    @data('user')
    def test_other_can_not_list_notifications(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class NotificationChangeTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_1 = factories.NotificationFactory(
            key='app_name.event_name', enabled=False
        )
        self.notification_2 = factories.NotificationFactory(
            key='app_name.event_name2', enabled=True
        )
        self.url = factories.NotificationFactory.get_url(self.notification_1)
        self.disable_url = factories.NotificationFactory.get_url(
            self.notification_2, action="disable"
        )
        self.enable_url = factories.NotificationFactory.get_url(
            self.notification_1, action="enable"
        )

    @data('staff')
    def test_staff_can_change_notifications(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        valid_data = {'key': 'appname.template_name'}

        response = self.client.put(self.url, valid_data)
        print(f"{response=}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_other_can_not_change_customer_division(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        valid_data = {'key': 'appname.template_name'}

        response = self.client.put(self.url, valid_data)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    @data('staff')
    def test_staff_can_disable_notifications(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.disable_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.notification_2.refresh_from_db()
        self.assertEqual(self.notification_2.enabled, False)

    @data('staff')
    def test_staff_can_enable_notifications(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.enable_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.notification_1.refresh_from_db()
        self.assertEqual(self.notification_1.enabled, True)

    @data('user')
    def test_other_can_not_disable_notifications(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.disable_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.notification_2.refresh_from_db()
        self.assertEqual(self.notification_2.enabled, True)

    @data('user')
    def test_other_can_not_enable_notifications(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.enable_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.notification_1.refresh_from_db()
        self.assertEqual(self.notification_1.enabled, False)


@ddt
class NotificationTemplateListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_template_1 = factories.NotificationTemplateFactory(
            path='marketplace/marketplace_plan_template.txt'
        )
        self.url = factories.NotificationTemplateFactory.get_list_url()
        self.override_url = factories.NotificationTemplateFactory.get_url(
            self.notification_template_1, action="override"
        )

    def tearDown(self):
        super().tearDown()
        Template.objects.all().delete()

    @data(
        'staff',
    )
    def test_staff_can_list_notification_templates(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        expected_template_content = get_template(
            self.notification_template_1.path
        ).template.source
        response = self.client.get(self.url)

        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(response.data[0]['path'], self.notification_template_1.path)
        self.assertEqual(response.data[0]['name'], self.notification_template_1.name)
        self.assertEqual(response.data[0]['content'], expected_template_content)

    @data(
        'user',
    )
    def test_other_can_not_list_notification_templates(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)

        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    @data(
        'staff',
    )
    def test_staff_can_override_notification_templates(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        Template.objects.create(name=self.notification_template_1.path)
        new_content = {'content': 'new_content'}
        response = self.client.post(self.override_url, new_content)

        self.assertEqual(status.HTTP_200_OK, response.status_code)

        updated_template_content = get_template(
            self.notification_template_1.path
        ).template.source

        response = self.client.get(self.url)
        self.assertEqual(response.data[0]['content'], updated_template_content)

    @data(
        'user',
    )
    def test_other_can_not_override_notification_templates(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        new_content = {'content': 'new_content'}
        response = self.client.post(self.override_url, new_content)

        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    @data(
        'staff',
    )
    def test_staff_can_not_override_notification_templates_that_does_not_exist(
        self, user
    ):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        new_content = {'content': 'new_content'}
        response = self.client.post(self.override_url, new_content)

        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)


@ddt
class NotificationTemplateFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_template_1 = factories.NotificationTemplateFactory(
            name='invitation_approved', path='users/invitation_approved_message.txt'
        )
        self.notification_template_2 = factories.NotificationTemplateFactory(
            name='invitation_rejected', path='users/invitation_rejected_message.txt'
        )
        self.url = factories.NotificationTemplateFactory.get_list_url()

    @data(
        'staff',
    )
    def test_notification_template_name_filter(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), 2)
        response = self.client.get(
            self.url,
            {'name': 'invitation'},
        )
        self.assertEqual(len(response.json()), 2)

    @data(
        'staff',
    )
    def test_notification_template_name_exact_filter(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), 2)
        response = self.client.get(
            self.url,
            {'name_exact': 'invitation_approved'},
        )
        self.assertEqual(len(response.json()), 1)
