from unittest import mock

from django.contrib.admin.models import ADDITION, LogEntry
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status, test

from waldur_core.core.models import SshPublicKey
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories


def events(event_type):
    return Event.objects.filter(event_type=event_type)


class AdminSiteLoginEventTest(TestCase):
    def setUp(self):
        cache.clear()
        self.password = "very-secret-password"
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.staff.set_password(self.password)
        self.staff.save()

    def tearDown(self):
        cache.clear()

    def login(self, password):
        return self.client.post(
            "/admin/login/", {"username": self.staff.username, "password": password}
        )

    def test_successful_login_is_logged(self):
        response = self.login(self.password)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        event = events(EventType.AUTH_LOGGED_IN_TO_ADMIN_SITE).get()
        self.assertEqual(event.context["user_uuid"], self.staff.uuid.hex)
        self.assertFalse(events(EventType.AUTH_LOGIN_FAILED_TO_ADMIN_SITE).exists())

    def test_wrong_password_is_logged(self):
        response = self.login("wrong-password")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event = events(EventType.AUTH_LOGIN_FAILED_TO_ADMIN_SITE).get()
        self.assertEqual(event.context["username"], self.staff.username)
        self.assertFalse(events(EventType.AUTH_LOGGED_IN_TO_ADMIN_SITE).exists())

    def test_repeated_failures_for_one_username_are_logged_once(self):
        for index in range(3):
            self.client.post(
                "/admin/login/",
                {"username": self.staff.username, "password": "wrong"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{index}",
            )

        self.assertEqual(events(EventType.AUTH_LOGIN_FAILED_TO_ADMIN_SITE).count(), 1)

    @mock.patch("waldur_core.logging.event_logger.FAILED_LOGIN_EVENTS_PER_MINUTE", 3)
    def test_failures_for_varying_usernames_are_capped(self):
        for index in range(5):
            self.client.post(
                "/admin/login/", {"username": f"user-{index}", "password": "wrong"}
            )

        self.assertEqual(events(EventType.AUTH_LOGIN_FAILED_TO_ADMIN_SITE).count(), 3)

    @mock.patch(
        "waldur_core.core.admin.admin_auth.user_can_satisfy_passkey",
        return_value=True,
    )
    @mock.patch(
        "waldur_core.core.admin.passkey_policy.is_enforced_for", return_value=True
    )
    def test_password_alone_is_not_logged_as_login_under_passkey_enforcement(
        self, *mocks
    ):
        response = self.login(self.password)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertFalse(events(EventType.AUTH_LOGGED_IN_TO_ADMIN_SITE).exists())

    def test_user_without_admin_access_is_logged(self):
        user = structure_factories.UserFactory()
        user.set_password(self.password)
        user.save()

        self.client.post(
            "/admin/login/", {"username": user.username, "password": self.password}
        )

        self.assertTrue(events(EventType.AUTH_LOGIN_FAILED_TO_ADMIN_SITE).exists())


class AdminActionEventTest(TestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def test_addition_is_logged(self):
        key = structure_factories.SshPublicKeyFactory()

        LogEntry.objects.log_actions(
            self.staff.pk, [key], ADDITION, "Added.", single_object=True
        )

        event = events(EventType.ADMIN_OBJECT_CREATED).get()
        self.assertEqual(event.context["admin_object_type"], "core.sshpublickey")
        self.assertEqual(event.context["admin_object_id"], str(key.pk))
        self.assertEqual(event.context["user_uuid"], self.staff.uuid.hex)

    def test_deletion_from_change_page_is_logged(self):
        key = structure_factories.SshPublicKeyFactory()

        response = self.client.post(
            f"/admin/core/sshpublickey/{key.pk}/delete/", {"post": "yes"}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertFalse(SshPublicKey.objects.filter(pk=key.pk).exists())
        event = events(EventType.ADMIN_OBJECT_DELETED).get()
        self.assertEqual(event.context["admin_object_id"], str(key.pk))

    def test_bulk_deletion_logs_every_object(self):
        keys = structure_factories.SshPublicKeyFactory.create_batch(2)

        response = self.client.post(
            "/admin/core/sshpublickey/",
            {
                "action": "delete_selected",
                "_selected_action": [key.pk for key in keys],
                "post": "yes",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertFalse(SshPublicKey.objects.filter(pk__in=[k.pk for k in keys]))
        self.assertEqual(
            sorted(
                events(EventType.ADMIN_OBJECT_DELETED).values_list(
                    "context__admin_object_id", flat=True
                )
            ),
            sorted(str(key.pk) for key in keys),
        )

    def test_bulk_deletion_confirmation_page_logs_nothing(self):
        keys = structure_factories.SshPublicKeyFactory.create_batch(2)

        self.client.post(
            "/admin/core/sshpublickey/",
            {
                "action": "delete_selected",
                "_selected_action": [key.pk for key in keys],
            },
        )

        self.assertFalse(events(EventType.ADMIN_OBJECT_DELETED).exists())


class TokenViewedEventTest(test.APITestCase):
    def test_staff_viewing_users_token_is_logged(self):
        staff = structure_factories.UserFactory(is_staff=True)
        user = structure_factories.UserFactory()
        self.client.force_authenticate(staff)

        response = self.client.get(f"/api/users/{user.uuid.hex}/token/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event = events(EventType.TOKEN_VIEWED).get()
        self.assertEqual(event.context["affected_user_uuid"], user.uuid.hex)
        self.assertEqual(event.context["user_uuid"], staff.uuid.hex)

    def test_staff_viewing_own_token_is_not_logged(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        response = self.client.get(f"/api/users/{staff.uuid.hex}/token/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(events(EventType.TOKEN_VIEWED).exists())
