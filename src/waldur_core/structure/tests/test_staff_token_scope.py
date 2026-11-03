"""A staff user's personal access token acts as staff only with the staff scope.

Scoped down to a few permissions, it must not pass staff-only checks: the
function permissions, `IsAdminOrOwner` and the checks written in views.
"""

import datetime

from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import User
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure import models
from waldur_core.structure.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

NARROW = [PermissionEnum.LIST_ORDERS.value]
STAFF = [PermissionEnum.STAFF_ACCESS.value]
SUPPORT = [PermissionEnum.SUPPORT_ACCESS.value]


def token_user(**flags):
    user = factories.UserFactory(can_use_personal_access_tokens=True, **flags)
    Token.objects.get_or_create(user=user)
    return user


@override_config(PAT_ENABLED=True)
class StaffTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.staff = token_user(is_staff=True)
        self.user = factories.UserFactory()

    def use_token(self, scopes, user=None):
        pat = _create_pat(user or self.staff, scopes=scopes, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))

    def grant_staff(self):
        return self.client.patch(
            factories.UserFactory.get_url(self.user), {"is_staff": True}
        )

    def change_password(self):
        return self.client.post(
            factories.UserFactory.get_url(self.user, action="change_password"),
            {"new_password": "Correct-horse-battery-9"},
        )

    def create_organization(self):
        return self.client.post(
            factories.CustomerFactory.get_list_url(), {"name": "New organization"}
        )

    def test_token_without_staff_scope_cannot_grant_staff(self):
        self.use_token(NARROW)
        # It reads as not staff (#987), so another user is not even found.
        self.assertEqual(self.grant_staff().status_code, status.HTTP_404_NOT_FOUND)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)

    def test_token_with_staff_scope_can_grant_staff(self):
        self.use_token(STAFF)
        self.assertEqual(self.grant_staff().status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)

    def test_token_without_staff_scope_cannot_change_a_password(self):
        self.use_token(NARROW)
        self.assertEqual(self.change_password().status_code, status.HTTP_404_NOT_FOUND)

    def test_token_with_staff_scope_can_change_a_password(self):
        self.use_token(STAFF)
        self.assertEqual(self.change_password().status_code, status.HTTP_200_OK)

    def test_token_without_staff_scope_cannot_create_an_organization(self):
        self.use_token(NARROW)
        self.assertEqual(
            self.create_organization().status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertFalse(
            models.Customer.objects.filter(name="New organization").exists()
        )

    def test_token_with_staff_scope_can_create_an_organization(self):
        self.use_token(STAFF)
        self.assertEqual(
            self.create_organization().status_code, status.HTTP_201_CREATED
        )

    def test_session_staff_is_unaffected(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.grant_staff().status_code, status.HTTP_200_OK)

    @override_config(FEDERATED_IDENTITY_SYNC_ENABLED=True)
    def test_token_without_staff_scope_cannot_push_identities(self):
        self.use_token(NARROW)
        response = self.client.post(
            "/api/identity-bridge/",
            {"username": self.user.username, "source": "isd:x", "email": "x@x.org"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.email, "x@x.org")

    @override_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="http://llm.invalid",
        AI_ASSISTANT_API_TOKEN="x",
    )
    def test_token_without_staff_scope_cannot_run_assistant_tools(self):
        self.use_token(NARROW)
        response = self.client.post(
            "/api/chat-tools/execute/",
            {"tool": "get_user_overview", "arguments": {"user_email": "x@x.org"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_config(PAT_ENABLED=True)
class SupportTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.resource = marketplace_fixtures.MarketplaceFixture().resource
        self.support = token_user(is_support=True)

    def set_end_date(self, scopes):
        pat = _create_pat(self.support, scopes=scopes, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        end_date = (timezone.now() + datetime.timedelta(days=10)).date()
        return self.client.post(
            f"/api/marketplace-provider-resources/{self.resource.uuid.hex}"
            "/set_end_date_by_provider/",
            {"end_date": end_date.isoformat()},
            format="json",
        )

    def test_token_without_support_scope_cannot_set_end_date(self):
        response = self.set_end_date(NARROW)
        # It reads as not support (#987), so the resource is not found.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.resource.refresh_from_db()
        self.assertIsNone(self.resource.end_date)

    def test_token_with_support_scope_can_set_end_date(self):
        response = self.set_end_date(SUPPORT)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@override_config(PAT_ENABLED=True)
class ApiTokenDisclosureTest(test.APITestCase):
    """The API token acts without a personal access token's scopes, so a
    personal access token must not read it, nor extend its lifetime."""

    def test_token_is_hidden_from_a_personal_access_token(self):
        for flags in ({"is_staff": True}, {}):
            with self.subTest(**flags):
                user = token_user(**flags)
                pat = _create_pat(user, scopes=NARROW, bindings=[])
                self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
                for url in ("/api/users/me/", factories.UserFactory.get_url(user)):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, status.HTTP_200_OK)
                    self.assertNotIn("token", response.data)
                    self.assertNotIn("token_lifetime", response.data)

    def test_personal_access_token_cannot_extend_the_token_lifetime(self):
        user = token_user(is_staff=True)
        user.token_lifetime = 3600
        user.save(update_fields=["token_lifetime"])
        pat = _create_pat(user, scopes=NARROW, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        self.client.patch(
            factories.UserFactory.get_url(user), {"token_lifetime": None}, format="json"
        )
        user = User.objects.get(pk=user.pk)
        self.assertEqual(user.token_lifetime, 3600)

    def test_session_still_sees_its_token(self):
        user = token_user()
        self.client.force_authenticate(user)
        response = self.client.get("/api/users/me/")
        self.assertIn("token", response.data)
