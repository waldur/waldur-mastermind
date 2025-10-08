from dbtemplates.models import Template
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories, fixtures


@ddt
class NotificationList(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.notification_1 = factories.NotificationFactory(key="app_name.event_name")
        self.notification_2 = factories.NotificationFactory(key="app_name.event_name2")
        self.url = factories.NotificationFactory.get_list_url()
        Template.objects.create(name="app_name/event_name_message.html")
        Template.objects.create(name="app_name/event_name_message.txt")
        Template.objects.create(name="app_name/event_name_subject.txt")
        Template.objects.create(name="app_name/event_name2_message.html")
        Template.objects.create(name="app_name/event_name2_message.txt")
        Template.objects.create(name="app_name/event_name2_subject.txt")

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
class NotificationChangeTest(test.APITransactionTestCase):
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
        Template.objects.create(name="app_name/event_name_message.html")
        Template.objects.create(name="app_name/event_name_message.txt")
        Template.objects.create(name="app_name/event_name_subject.txt")

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
class NotificationTemplateListTest(test.APITransactionTestCase):
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

        template = Template.objects.create(name=self.template.path)
        new_content = {"content": "new_content"}

        response = self.client.post(self.override_url, new_content)
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        response = self.client.get(self.url)
        template.refresh_from_db()
        self.assertEqual(response.data[0]["content"], template.content)

    @data("staff")
    def test_staff_cannot_override_template_with_invalid_content(self, user):
        """
        Verify that updating a template with invalid Django template syntax fails
        in the update (override) view.
        """
        self.client.force_authenticate(getattr(self.fixture, user))

        # Create an initial template in the DB to override
        original_content = "This is the original content."
        db_template = Template.objects.create(
            name=self.template.path, content=original_content
        )

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
        db_template.refresh_from_db()
        self.assertEqual(db_template.content, original_content)

    @data("staff")
    def test_list_view_handles_invalid_db_content(self, user):
        """
        Verify that the list view still successfully returns content (the raw string)
        even if the content stored in the DB is syntactically invalid.
        """
        self.client.force_authenticate(getattr(self.fixture, user))

        # 1. Store syntactically INVALID content directly into the DB template model
        # Note: This simulates a template being saved with errors, perhaps via raw SQL
        # or an old process without validation.
        INVALID_CONTENT = (
            "This content has a syntax error: {% if invitation['type'] == 'project' %}"
        )
        Template.objects.create(name=self.template.path, content=INVALID_CONTENT)

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
class NotificationTemplateFilterTest(test.APITransactionTestCase):
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
