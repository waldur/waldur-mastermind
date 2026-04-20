from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import tasks
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories, fixtures

VALID_PDF = (
    b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\n"
    b"xref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n"
    b"/Root 1 0 R\n>>\nstartxref\n9\n%%EOF"
)


def _make_pdf(name="test.pdf"):
    return SimpleUploadedFile(name, VALID_PDF, content_type="application/pdf")


class BaseProviderConsumerInfoTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_CONSUMER_ORDER_INFO)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.SET_CONSUMER_ORDER_INFO)
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options["enable_provider_consumer_messaging"] = True
        self.offering.save(update_fields=["plugin_options"])
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save(update_fields=["state"])


# ── Toggle tests ──


class ToggleDisabledTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_CONSUMER_ORDER_INFO)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save(update_fields=["state"])

    def test_set_provider_info_fails_when_messaging_disabled(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.OrderFactory.get_url(self.order, "set_provider_info")
        response = self.client.post(url, {"provider_message": "hello"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_set_consumer_info_fails_when_messaging_disabled(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OrderFactory.get_url(self.order, "set_consumer_info")
        response = self.client.post(url, {"consumer_message": "hello"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


# ── set_provider_info tests ──


class SetProviderInfoTest(BaseProviderConsumerInfoTest):
    def _post(self, user, data=None, format=None):
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "set_provider_info")
        kwargs = {}
        if format:
            kwargs["format"] = format
        return self.client.post(url, data or {}, **kwargs)

    def test_provider_can_set_message(self):
        response = self._post(
            self.fixture.offering_owner,
            {"provider_message": "Please sign NDA"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.provider_message, "Please sign NDA")

    def test_provider_can_set_url(self):
        response = self._post(
            self.fixture.offering_owner,
            {
                "provider_message": "See link",
                "provider_message_url": "https://example.com/nda",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.provider_message_url, "https://example.com/nda")

    def test_provider_can_upload_pdf(self):
        response = self._post(
            self.fixture.offering_owner,
            {
                "provider_message": "Attached",
                "provider_message_attachment": _make_pdf(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertTrue(self.order.provider_message_attachment)

    def test_unauthorized_user_cannot_set_info(self):
        user = structure_factories.UserFactory()
        response = self._post(user, {"provider_message": "hello"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_fails_in_wrong_state(self):
        self.order.state = OrderStates.DONE
        self.order.save(update_fields=["state"])
        response = self._post(
            self.fixture.offering_owner,
            {"provider_message": "hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_can_update_multiple_times(self):
        self._post(
            self.fixture.offering_owner,
            {"provider_message": "First message"},
        )
        self._post(
            self.fixture.offering_owner,
            {"provider_message": "Updated message"},
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.provider_message, "Updated message")

    def test_fields_visible_in_detail_response(self):
        self._post(
            self.fixture.offering_owner,
            {"provider_message": "Check this"},
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.get(url)
        self.assertEqual(response.data["provider_message"], "Check this")

    def test_output_updated_at_is_visible_in_detail_response(self):
        self.order.output = "Provider processing output"
        self.order.save(update_fields=["output"])

        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("output_updated_at", response.data)
        self.assertIsNotNone(response.data["output_updated_at"])

    def test_consumer_cannot_use_provider_endpoint(self):
        response = self._post(
            self.fixture.owner,
            {"provider_message": "hello from consumer"},
        )
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    def test_attachment_replaced_on_reupload(self):
        self._post(
            self.fixture.offering_owner,
            {
                "provider_message": "First",
                "provider_message_attachment": _make_pdf("first.pdf"),
            },
            format="multipart",
        )
        self.order.refresh_from_db()
        first_path = self.order.provider_message_attachment.name

        self._post(
            self.fixture.offering_owner,
            {
                "provider_message": "Second",
                "provider_message_attachment": _make_pdf("second.pdf"),
            },
            format="multipart",
        )
        self.order.refresh_from_db()
        second_path = self.order.provider_message_attachment.name
        self.assertNotEqual(first_path, second_path)


# ── set_consumer_info tests ──


class SetConsumerInfoTest(BaseProviderConsumerInfoTest):
    def _post(self, user, data=None, format=None):
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "set_consumer_info")
        kwargs = {}
        if format:
            kwargs["format"] = format
        return self.client.post(url, data or {}, **kwargs)

    def test_consumer_can_respond(self):
        response = self._post(
            self.fixture.owner,
            {"consumer_message": "Signed document attached"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.consumer_message, "Signed document attached")

    def test_project_manager_can_respond(self):
        response = self._post(
            self.fixture.manager,
            {"consumer_message": "Responding as project manager"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.consumer_message, "Responding as project manager")

    def test_consumer_can_upload_pdf(self):
        response = self._post(
            self.fixture.owner,
            {
                "consumer_message": "Here is the signed NDA",
                "consumer_message_attachment": _make_pdf(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertTrue(self.order.consumer_message_attachment)

    def test_unauthorized_user_cannot_respond(self):
        user = structure_factories.UserFactory()
        response = self._post(user, {"consumer_message": "hello"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_fails_in_wrong_state(self):
        self.order.state = OrderStates.DONE
        self.order.save(update_fields=["state"])
        response = self._post(
            self.fixture.owner,
            {"consumer_message": "hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_can_update_multiple_times(self):
        self._post(
            self.fixture.owner,
            {"consumer_message": "First"},
        )
        self._post(
            self.fixture.owner,
            {"consumer_message": "Re-uploaded"},
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.consumer_message, "Re-uploaded")

    def test_fields_visible_in_detail_response(self):
        self._post(
            self.fixture.owner,
            {"consumer_message": "My response"},
        )
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.get(url)
        self.assertEqual(response.data["consumer_message"], "My response")

    def test_provider_cannot_use_consumer_endpoint(self):
        response = self._post(
            self.fixture.offering_owner,
            {"consumer_message": "hello from provider"},
        )
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    def test_attachment_replaced_on_reupload(self):
        self._post(
            self.fixture.owner,
            {
                "consumer_message": "First",
                "consumer_message_attachment": _make_pdf("first.pdf"),
            },
            format="multipart",
        )
        self.order.refresh_from_db()
        first_path = self.order.consumer_message_attachment.name

        self._post(
            self.fixture.owner,
            {
                "consumer_message": "Second",
                "consumer_message_attachment": _make_pdf("second.pdf"),
            },
            format="multipart",
        )
        self.order.refresh_from_db()
        second_path = self.order.consumer_message_attachment.name
        self.assertNotEqual(first_path, second_path)


# ── Notification tests ──


@override_settings(task_always_eager=True)
class ProviderInfoNotificationTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options["enable_provider_consumer_messaging"] = True
        self.offering.plugin_options["notify_about_provider_consumer_messages"] = True
        self.offering.save(update_fields=["plugin_options"])
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save(update_fields=["state"])

    def test_provider_info_triggers_consumer_email(self):
        structure_factories.NotificationFactory(
            key="marketplace.notify_consumer_about_provider_info"
        )
        tasks.notify_consumer_about_provider_info(self.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.order.created_by.email, mail.outbox[0].to)

    def test_consumer_info_triggers_provider_email(self):
        # Access offering_owner to ensure the user is created before the task runs
        offering_owner = self.fixture.offering_owner
        structure_factories.NotificationFactory(
            key="marketplace.notify_provider_about_consumer_info"
        )
        tasks.notify_provider_about_consumer_info(self.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [offering_owner.email])

    def test_no_email_when_notification_option_disabled(self):
        self.offering.plugin_options["notify_about_provider_consumer_messages"] = False
        self.offering.save(update_fields=["plugin_options"])
        structure_factories.NotificationFactory(
            key="marketplace.notify_consumer_about_provider_info"
        )
        tasks.notify_consumer_about_provider_info(self.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 0)
