from datetime import datetime
from unittest import mock

from ddt import data, ddt
from django.conf import settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.core.models import User
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.core.utils import format_homeport_link
from waldur_core.logging import models as logging_models
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures

from .. import tasks


class UserPermissionApiTest(test.APITestCase):
    def setUp(self):
        self.users = {
            "staff": factories.UserFactory(
                is_staff=True, agreement_date=timezone.now()
            ),
            "owner": factories.UserFactory(agreement_date=timezone.now()),
            "not_owner": factories.UserFactory(agreement_date=timezone.now()),
            "other": factories.UserFactory(agreement_date=timezone.now()),
        }
        self.customer = factories.CustomerFactory()
        self.customer.add_user(self.users["owner"], CustomerRole.OWNER)
        self.customer.add_user(self.users["not_owner"], CustomerRole.SUPPORT)

    # List filtration tests
    def test_anonymous_user_cannot_list_accounts(self):
        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authorized_user_can_list_accounts(self):
        self.client.force_authenticate(self.users["owner"])

        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_user_can_list_accounts(self):
        self.client.force_authenticate(user=self.users["staff"])

        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_cannot_see_token_in_the_list(self):
        self.client.force_authenticate(self.users["staff"])

        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(self.users))
        self.assertIsNone(response.data[0].get("token"))

    def test_staff_cannot_see_token_and_its_lifetime_of_the_other_user(self):
        self.client.force_authenticate(self.users["staff"])

        response = self.client.get(factories.UserFactory.get_url(self.users["owner"]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data.get("token"))
        self.assertNotIn("token_lifetime", response.data)

    def test_owner_cannot_see_token_and_its_lifetime_field_in_the_list_of_users(self):
        self.client.force_authenticate(self.users["owner"])

        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertIsNot("token", response.data[0])
        self.assertIsNot("token_lifetime", response.data[0])

    def test_owner_can_see_his_token_and_its_lifetime(self):
        self.client.force_authenticate(self.users["owner"])

        response = self.client.get(factories.UserFactory.get_url(self.users["owner"]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data["token"])
        self.assertIn("token_lifetime", response.data)

    def test_owner_cannot_see_token_and_its_lifetime_of_the_not_owner(self):
        self.client.force_authenticate(self.users["owner"])

        response = self.client.get(
            factories.UserFactory.get_url(self.users["not_owner"])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("token", response.data)
        self.assertNotIn("token_lifetime", response.data)

    def test_owner_cannot_see_other_user(self):
        self.client.force_authenticate(self.users["owner"])

        response = self.client.get(factories.UserFactory.get_url(self.users["other"]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_see_his_token_via_current_filter(self):
        self.client.force_authenticate(self.users["owner"])

        response = self.client.get(factories.UserFactory.get_list_url("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone("token", response.data)
        self.assertIsNotNone("token_lifetime", response.data)

    def test_me_endpoint_includes_session_fields_for_regular_user(self):
        user = factories.UserFactory()
        self.client.force_authenticate(user)

        response = self.client.get(factories.UserFactory.get_list_url("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("has_active_session", response.data)
        self.assertIn("has_usable_password", response.data)
        self.assertIsInstance(response.data["has_active_session"], bool)
        self.assertIsInstance(response.data["has_usable_password"], bool)

    def test_me_endpoint_includes_ip_address_from_x_forwarded_for(self):
        self.client.force_authenticate(self.users["owner"])
        response = self.client.get(
            factories.UserFactory.get_list_url("me"),
            HTTP_X_FORWARDED_FOR="192.168.1.100, 10.0.0.1",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("ip_address", response.data)
        self.assertEqual(response.data["ip_address"], "192.168.1.100")

    def test_me_endpoint_includes_ip_address_from_remote_addr(self):
        self.client.force_authenticate(self.users["owner"])
        response = self.client.get(
            factories.UserFactory.get_list_url("me"), REMOTE_ADDR="192.168.1.50"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("ip_address", response.data)
        self.assertEqual(response.data["ip_address"], "192.168.1.50")

    @mock.patch("waldur_core.structure.views.get_ip_address")
    def test_me_endpoint_includes_none_ip_address_when_no_headers(self, mock_get_ip):
        mock_get_ip.return_value = None
        self.client.force_authenticate(self.users["owner"])
        response = self.client.get(factories.UserFactory.get_list_url("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("ip_address", response.data)
        self.assertIsNone(response.data["ip_address"])

    # Creation tests
    def test_anonymous_user_cannot_create_account(self):
        data = self._get_valid_payload()

        response = self.client.post(factories.UserFactory.get_list_url(), data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authorized_user_cannot_create_account(self):
        self.client.force_authenticate(self.users["owner"])

        data = self._get_valid_payload()

        response = self.client.post(factories.UserFactory.get_list_url(), data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_user_can_create_account(self):
        self.client.force_authenticate(self.users["staff"])

        data = self._get_valid_payload()

        response = self.client.post(factories.UserFactory.get_list_url(), data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        created_user = User.objects.filter(username=data["username"]).first()
        self.assertIsNotNone(created_user, "User should have been created")

    def test_staff_user_cannot_set_civil_number_upon_account_creation(self):
        self.client.force_authenticate(self.users["staff"])

        data = self._get_valid_payload()
        data["civil_number"] = "foobar"

        response = self.client.post(factories.UserFactory.get_list_url(), data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        created_user = User.objects.get(username=data["username"])
        self.assertIsNone(
            created_user.civil_number, "User's civil_number should be unset"
        )

    # Manipulation tests
    def test_user_can_change_his_account_email(self):
        data = {
            "email": "example@example.com",
            "phone_number": "123456789",
        }

        self._ensure_user_can_change_field(self.users["owner"], "phone_number", data)

    def test_user_cannot_change_other_account_email(self):
        data = {
            "email": "example@example.com",
            "phone_number": "123456789",
        }

        self._ensure_user_cannot_change_field(self.users["owner"], "phone_number", data)

    def test_user_cannot_make_himself_support(self):
        user = factories.UserFactory(agreement_date=timezone.now())
        url = factories.UserFactory.get_url(user)
        self.client.force_authenticate(user)

        response = self.client.patch(url, {"is_support": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user.refresh_from_db()
        self.assertFalse(user.is_support)

    def test_support_cannot_make_himself_staff(self):
        user = factories.UserFactory(agreement_date=timezone.now(), is_support=True)
        self.client.force_authenticate(user)
        url = factories.UserFactory.get_url(user)

        response = self.client.patch(url, {"is_staff": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        user.refresh_from_db()
        self.assertFalse(user.is_staff)

    def test_staff_user_cannot_change_civil_number(self):
        self.client.force_authenticate(self.users["staff"])

        user = factories.UserFactory()

        data = self._get_valid_payload(user)
        data["civil_number"] = "foobar"

        self.client.put(factories.UserFactory.get_url(user), data)

        reread_user = User.objects.get(username=data["username"])
        self.assertEqual(
            reread_user.civil_number,
            user.civil_number,
            "User's civil_number should be left intact",
        )

    def test_user_can_change_his_token_lifetime(self):
        data = {
            "email": "example@example.com",
            "token_lifetime": 100,
        }

        self._ensure_user_can_change_field(self.users["owner"], "token_lifetime", data)

    def test_user_cannot_change_other_token_lifetime(self):
        data = {
            "email": "example@example.com",
            "token_lifetime": 100,
        }

        self._ensure_user_cannot_change_field(
            self.users["owner"], "token_lifetime", data
        )

    def test_staff_user_can_change_any_accounts_fields(self):
        self.client.force_authenticate(user=self.users["staff"])
        data = self._get_valid_payload()

        response = self.client.put(
            factories.UserFactory.get_url(self.users["staff"]), data
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_cannot_grant_pat_access_to_himself(self):
        owner = self.users["owner"]
        self.assertFalse(owner.can_use_personal_access_tokens)
        self.client.force_authenticate(user=owner)

        response = self.client.patch(
            factories.UserFactory.get_url(owner),
            {"can_use_personal_access_tokens": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        owner.refresh_from_db()
        self.assertFalse(owner.can_use_personal_access_tokens)

    def test_staff_can_grant_pat_access_to_user(self):
        owner = self.users["owner"]
        self.assertFalse(owner.can_use_personal_access_tokens)
        self.client.force_authenticate(user=self.users["staff"])

        response = self.client.patch(
            factories.UserFactory.get_url(owner),
            {"can_use_personal_access_tokens": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        owner.refresh_from_db()
        self.assertTrue(owner.can_use_personal_access_tokens)

    # Deletion tests
    def user_cannot_delete_his_account(self):
        self._ensure_user_cannot_delete_account(
            self.users["owner"], self.users["owner"]
        )

    def user_cannot_delete_other_account(self):
        self._ensure_user_cannot_delete_account(
            self.users["not_owner"], self.users["owner"]
        )

    def test_staff_user_can_delete_any_account(self):
        self.client.force_authenticate(user=self.users["staff"])

        for user in self.users:
            response = self.client.delete(
                factories.UserFactory.get_url(self.users[user])
            )
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    # Helper methods
    def _get_valid_payload(self, account=None):
        account = account or factories.UserFactory.build()

        return {
            "username": account.username,
            "email": account.email,
            "full_name": account.full_name,
            "native_name": account.native_name,
            "is_staff": account.is_staff,
            "is_active": account.is_active,
        }

    def _get_null_payload(self, account=None):
        account = account or factories.UserFactory.build()

        return {
            "username": account.username,
            "email": account.email,
            "full_name": None,
            "native_name": None,
            "phone_number": None,
            "description": None,
            "is_staff": account.is_staff,
            "is_active": account.is_active,
        }

    def _ensure_user_can_change_field(self, user, field_name, data):
        self.client.force_authenticate(user)

        response = self.client.put(factories.UserFactory.get_url(user), data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        new_value = getattr(User.objects.get(uuid=user.uuid), field_name)
        self.assertEqual(new_value, data[field_name])

    def _ensure_user_cannot_change_field(self, user, field_name, data):
        self.client.force_authenticate(user)

        response = self.client.put(
            factories.UserFactory.get_url(self.users["not_owner"]), data
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        new_value = getattr(
            User.objects.get(uuid=self.users["not_owner"].uuid), field_name
        )
        self.assertNotEqual(new_value, data[field_name])

    def _ensure_user_cannot_delete_account(self, user, account):
        self.client.force_authenticate(user=user)

        response = self.client.delete(factories.UserFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UserPermissionApiListTest(test.APITestCase):
    def setUp(self):
        self.users = {
            "staff": factories.UserFactory(
                is_staff=True, agreement_date=timezone.now()
            ),
            "owner": factories.UserFactory(agreement_date=timezone.now()),
            "other": factories.UserFactory(agreement_date=timezone.now()),
        }

    def test_staff_can_view_all_users(self):
        self.client.force_authenticate(self.users["staff"])
        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(len(response.data), len(self.users))

    def test_owner_can_view_other_users_in_organization(self):
        self.client.force_authenticate(self.users["owner"])
        customer = factories.CustomerFactory()
        customer.add_user(self.users["owner"], CustomerRole.OWNER)
        customer.add_user(self.users["other"], CustomerRole.SUPPORT)
        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(len(response.data), 2)

    def test_owner_can_view_other_users_in_project(self):
        self.client.force_authenticate(self.users["other"])

        project = factories.ProjectFactory()
        project.add_user(self.users["owner"], ProjectRole.MANAGER)
        project.add_user(self.users["other"], ProjectRole.MEMBER)

        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(len(response.data), 2)

    def test_owner_cannot_view_other_users(self):
        self.client.force_authenticate(self.users["owner"])
        response = self.client.get(factories.UserFactory.get_list_url())
        self.assertEqual(len(response.data), 1)


class UserFilterTest(test.APITestCase):
    def test_user_list_can_be_filtered(self):
        supported_filters = [
            "full_name",
            "native_name",
            "organization",
            "email",
            "phone_number",
            "description",
            "job_title",
            "username",
            "civil_number",
            "is_active",
        ]
        user = factories.UserFactory(is_staff=True)
        user_that_should_be_found = factories.UserFactory(
            native_name="",
            organization="",
            email="none@example.com",
            phone_number="",
            description="",
            job_title="",
            username="",
            civil_number="",
            is_active=False,
        )
        self.client.force_authenticate(user)
        url = factories.UserFactory.get_list_url()
        user_url = factories.UserFactory.get_url(user)
        user_that_should_not_be_found_url = factories.UserFactory.get_url(
            user_that_should_be_found
        )

        for field in supported_filters:
            response = self.client.get(url, data={field: getattr(user, field)})
            self.assertContains(response, user_url)
            self.assertNotContains(response, user_that_should_not_be_found_url)

    def test_user_list_can_be_filtered_by_fields_with_partial_matching(self):
        supported_filters = [
            "full_name",
            "native_name",
            "email",
            "job_title",
        ]
        user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user)
        url = factories.UserFactory.get_list_url()
        user_url = factories.UserFactory.get_url(user)

        for field in supported_filters:
            response = self.client.get(url, data={field: getattr(user, field)[:-1]})
            self.assertContains(response, user_url)

    def test_use_query_parameter_to_filtering_by_full_name(self):
        user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user)
        url = factories.UserFactory.get_list_url()
        user_url = factories.UserFactory.get_url(user)
        response = self.client.get(url, data={"query": user.full_name})
        self.assertContains(response, user_url)

    def test_user_list_can_be_filtered_with_accents(self):
        user = factories.UserFactory(is_staff=True, full_name="François Jürimäe")
        self.client.force_authenticate(user)
        url = factories.UserFactory.get_list_url()
        user_url = factories.UserFactory.get_url(user)

        response = self.client.get(url, data={"full_name": "François Jürimäe"})
        self.assertContains(response, user_url)

    def test_user_list_can_be_filtered_without_accents(self):
        user = factories.UserFactory(is_staff=True, full_name="François Jürimäe")
        self.client.force_authenticate(user)
        url = factories.UserFactory.get_list_url()
        user_url = factories.UserFactory.get_url(user)

        response = self.client.get(url, data={"full_name": "Francois Jurimae"})
        self.assertContains(response, user_url)

    @override_waldur_core_settings(
        LOCAL_IDP_NAME="Local DB (name)",
        LOCAL_IDP_LABEL="Local DB (label)",
        LOCAL_IDP_MANAGEMENT_URL="http://local-db.example.com/user-details/",
        LOCAL_IDP_PROTECTED_FIELDS=["full_name"],
    )
    def test_user_identity_provider_data(self):
        user = factories.UserFactory(is_staff=True, full_name="François Jürimäe")
        self.client.force_authenticate(user)
        user_url = factories.UserFactory.get_url(user)

        response = self.client.get(user_url)
        user = response.data
        self.assertEqual(
            settings.WALDUR_CORE["LOCAL_IDP_NAME"], user["identity_provider_name"]
        )
        self.assertEqual(
            settings.WALDUR_CORE["LOCAL_IDP_LABEL"], user["identity_provider_label"]
        )
        self.assertEqual(
            settings.WALDUR_CORE["LOCAL_IDP_MANAGEMENT_URL"],
            user["identity_provider_management_url"],
        )
        self.assertEqual(
            settings.WALDUR_CORE["LOCAL_IDP_PROTECTED_FIELDS"],
            user["identity_provider_fields"],
        )


class CustomUsersFilterTest(test.APITestCase):
    def setUp(self):
        fixture = fixtures.ProjectFixture()
        self.customer1 = fixture.customer
        self.project1 = fixture.project
        self.staff = fixture.staff
        self.owner1 = fixture.owner
        self.manager1 = fixture.manager

        fixture2 = fixtures.ProjectFixture()
        self.customer2 = fixture2.customer
        self.project2 = fixture2.project
        self.owner2 = fixture2.owner
        self.manager2 = fixture2.manager

        self.client.force_authenticate(self.staff)
        self.url = factories.UserFactory.get_list_url()

    def test_filter_user_by_customer(self):
        response = self.client.get(self.url, {"customer_uuid": self.customer1.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        actual = {user["uuid"] for user in response.data}
        expected = {self.owner1.uuid.hex, self.manager1.uuid.hex}
        self.assertEqual(actual, expected)

    def test_filter_user_by_project(self):
        response = self.client.get(self.url, {"project_uuid": self.project1.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        actual = [user["uuid"] for user in response.data]
        expected = [self.manager1.uuid.hex]
        self.assertEqual(actual, expected)


@ddt
@freeze_time("2017-01-19")
class UserUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.user = self.fixture.user
        self.staff = self.fixture.staff
        self.client.force_authenticate(self.user)
        self.url = factories.UserFactory.get_url(self.user)

        self.invalid_payload = {"phone_number": "123456789"}
        self.valid_payload = dict(agree_with_policy=True, **self.invalid_payload)

    def test_if_user_did_not_accept_policy_he_can_not_update_his_profile(self):
        response = self.client.put(self.url, self.invalid_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["agree_with_policy"], ["User must agree with the policy."]
        )

    def test_if_user_is_staff_he_can_update_his_profile_without_accepting_policy(self):
        self.user.is_staff = True
        self.user.save()
        response = self.client.patch(self.url, self.invalid_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["phone_number"], self.invalid_payload["phone_number"]
        )

    def test_if_user_already_accepted_policy_he_can_update_his_profile(self):
        self.user.agreement_date = timezone.now()
        self.user.save()

        response = self.client.put(self.url, self.invalid_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_if_user_accepts_policy_he_can_update_his_profile(self):
        response = self.client.put(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, self.valid_payload["phone_number"])

    def test_if_user_accepts_policy_agreement_data_is_updated(self):
        response = self.client.put(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertAlmostEqual(self.user.agreement_date, timezone.now())

    def test_token_lifetime_cannot_be_less_than_60_seconds(self):
        self.valid_payload["token_lifetime"] = 59

        response = self.client.put(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("token_lifetime", response.data)

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_user_can_not_update_profile_if_registration_method_is_protected(self):
        # Arrange
        self.user.registration_method = "PROTECTED"
        self.user.save()

        # Act
        self.valid_payload["organization"] = "New org"
        self.client.put(self.url, self.valid_payload)

        # Assert
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.organization, "New org")

    def test_deactivation_of_user_if_policy_has_not_been_accepted(self):
        self.user.agreement_date = None
        self.user.is_active = True
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(self.url, {"is_active": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.is_active, False)

    def test_deactivation_of_user_if_policy_has_been_accepted(self):
        self.user.agreement_date = timezone.now()
        self.user.is_active = True
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(self.url, {"is_active": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.is_active, False)

    def test_staff_deactivation_sets_default_deactivation_reason(self):
        self.user.agreement_date = timezone.now()
        self.user.is_active = True
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(self.url, {"is_active": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(
            self.user.deactivation_reason,
            f"Manually deactivated by {self.staff.username}",
        )

    def test_staff_deactivation_with_custom_reason(self):
        self.user.agreement_date = timezone.now()
        self.user.is_active = True
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            self.url,
            {"is_active": False, "deactivation_reason": "Violated terms of service"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.deactivation_reason, "Violated terms of service")

    def test_staff_reactivation_clears_deactivation_reason(self):
        self.user.is_active = False
        self.user.deactivation_reason = "Manually deactivated by admin"
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(self.url, {"is_active": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.deactivation_reason, "")

    def test_deactivation_reason_visible_to_staff(self):
        self.user.is_active = False
        self.user.deactivation_reason = "All roles were revoked"
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["deactivation_reason"], "All roles were revoked")

    def test_deactivation_reason_visible_to_support(self):
        support = self.fixture.global_support
        self.user.is_active = False
        self.user.deactivation_reason = "All roles were revoked"
        self.user.save()

        self.client.force_authenticate(support)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["deactivation_reason"], "All roles were revoked")

    def test_deactivation_reason_hidden_from_regular_users(self):
        other_user = factories.UserFactory(agreement_date=timezone.now())
        self.client.force_authenticate(other_user)
        response = self.client.get(self.url)
        self.assertNotIn("deactivation_reason", response.data)

    def test_staff_deactivation_sets_admin_override_flag(self):
        self.user.agreement_date = timezone.now()
        self.user.is_active = True
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(self.url, {"is_active": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.user.is_admin_deactivated)

    def test_staff_reactivation_clears_admin_override_flag(self):
        self.user.is_active = False
        self.user.is_admin_deactivated = True
        self.user.deactivation_reason = "Manually deactivated by admin"
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(self.url, {"is_active": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertFalse(self.user.is_admin_deactivated)

    def test_admin_deactivated_flag_visible_to_staff(self):
        self.user.is_active = False
        self.user.is_admin_deactivated = True
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_admin_deactivated"])

    def test_admin_deactivated_flag_hidden_from_regular_users(self):
        other_user = factories.UserFactory(agreement_date=timezone.now())
        self.client.force_authenticate(other_user)
        response = self.client.get(self.url)
        self.assertNotIn("is_admin_deactivated", response.data)

    def test_regular_user_cannot_set_admin_override_flag(self):
        # is_admin_deactivated is read-only; a client cannot toggle it directly.
        self.user.agreement_date = timezone.now()
        self.user.is_active = True
        self.user.save()

        self.client.force_authenticate(self.staff)
        response = self.client.patch(self.url, {"is_admin_deactivated": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        # Flag only flips as a side effect of an is_active=False change.
        self.assertFalse(self.user.is_admin_deactivated)

    @override_waldur_core_settings(LOCAL_IDP_PROTECTED_FIELDS=["full_name"])
    def test_user_can_update_only_allowed_fields(self):
        payload = {
            "full_name": "New User",
        }
        old_name = self.user.full_name
        self.user.agreement_date = datetime.now()
        self.user.save()
        response = self.client.patch(self.url, payload)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.user.refresh_from_db()
        self.assertEqual(old_name, self.user.full_name)

    @data("user", "staff")
    def test_user_can_upload_image(self, user):
        self.user.agreement_date = timezone.now()
        self.user.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(
            self.url, {"image": dummy_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.image)

    @data("global_support")
    def test_user_cannot_upload_image(self, user):
        self.user.agreement_date = timezone.now()
        self.user.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(
            self.url, {"image": dummy_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UserConfirmEmailTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = fixtures.UserFixture()
        self.user = fixture.user
        self.client.force_authenticate(self.user)
        self.url = factories.UserFactory.get_url(self.user, "change_email")

        self.valid_payload = {
            "email": "updatedmail@example.com",
        }

    def test_change_email_request_is_created_if_it_does_not_exist_yet(self):
        self.assertFalse(getattr(self.user, "changeemailrequest", False))
        response = self.client.post(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertNotEqual(self.user.email, self.valid_payload["email"])
        self.assertTrue(self.user.changeemailrequest)

    def test_change_email_request_is_created_if_email_exists_already(self):
        other_user = factories.UserFactory()
        valid_payload = {
            "email": other_user.email,
        }
        response = self.client.post(self.url, valid_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_change_email_request_is_not_created_if_email_is_invalid(self):
        self.valid_payload["email"] = "invalid_email"
        response = self.client.post(self.url, self.valid_payload)
        self.assertFalse(hasattr(self.user, "changeemailrequest"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["email"], ["Enter a valid email address."])

    def test_when_change_email_request_is_confirmed_user_email_is_updated(self):
        self.client.post(self.url, self.valid_payload)
        url = factories.UserFactory.get_list_url("confirm_email")
        response = self.client.post(
            url, {"code": self.user.changeemailrequest.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "updatedmail@example.com")

    def test_when_two_users_created_requests_with_equal_emails_then_first_confirmed_request_will_be_executed_second_will_be_deleted(
        self,
    ):
        self.client.post(self.url, self.valid_payload)
        self.assertEqual(
            self.valid_payload["email"], self.user.changeemailrequest.email
        )

        other_user = factories.UserFactory()
        self.client.force_authenticate(other_user)
        other_url = factories.UserFactory.get_url(other_user, "change_email")
        self.client.post(other_url, self.valid_payload)
        self.assertEqual(
            self.valid_payload["email"], other_user.changeemailrequest.email
        )

        self.client.force_authenticate(self.user)
        confirm_url = factories.UserFactory.get_list_url("confirm_email")
        response = self.client.post(
            confirm_url, {"code": self.user.changeemailrequest.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(other_user)
        other_confirm_url = factories.UserFactory.get_url(other_user, "confirm_email")
        response = self.client.post(
            other_confirm_url, {"code": other_user.changeemailrequest.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @freeze_time("2017-01-19")
    def test_validate_email_change_max_age(self):
        self.client.post(self.url, self.valid_payload)
        self.assertEqual(
            self.valid_payload["email"], self.user.changeemailrequest.email
        )
        url = factories.UserFactory.get_list_url("confirm_email")

        with freeze_time("2017-01-21"):
            response = self.client.post(
                url, {"code": self.user.changeemailrequest.uuid.hex}
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data[0], "Request has expired.")

    def test_anonymous_user_can_confirm_email(self):
        self.client.post(self.url, self.valid_payload)
        self.assertEqual(
            self.valid_payload["email"], self.user.changeemailrequest.email
        )
        self.client.force_authenticate(None)
        url = factories.UserFactory.get_list_url("confirm_email")
        response = self.client.post(
            url, {"code": self.user.changeemailrequest.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @mock.patch("waldur_core.structure.handlers.tasks")
    def test_send_mail_notification(self, mock_tasks):
        response = self.client.post(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_tasks.send_change_email_notification.delay.call_count, 1)
        self.assertEqual(
            mock_tasks.send_change_email_notification.delay.call_args[0][0],
            core_utils.serialize_instance(self.user.changeemailrequest),
        )

    @mock.patch("waldur_core.structure.handlers.tasks.core_utils.broadcast_mail")
    def test_send_change_email_notification_task(self, mock_mail):
        self.client.post(self.url, self.valid_payload)
        self.assertTrue(self.user.changeemailrequest)
        request_serialized = core_utils.serialize_instance(self.user.changeemailrequest)
        tasks.send_change_email_notification(request_serialized)

        link = format_homeport_link(
            "user_email_change/{code}/", code=self.user.changeemailrequest.uuid.hex
        )
        context = {"request": self.user.changeemailrequest, "link": link}
        mock_mail.assert_called_once_with(
            "structure",
            "change_email_request",
            context,
            [self.user.changeemailrequest.email],
        )

    def test_cancel_change_email(self):
        self.client.post(self.url, self.valid_payload)
        self.user.refresh_from_db()
        self.assertTrue(self.user.changeemailrequest)

        cancel_url = factories.UserFactory.get_url(self.user, "cancel_change_email")
        self.client.post(cancel_url)
        self.user.refresh_from_db()
        self.assertFalse(getattr(self.user, "changeemailrequest", False))


@ddt
class UserFullnameTest(test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory()

    @data(
        ("", "", ""),
        ("John", "John", ""),
        ("John Smith", "John", "Smith"),
        ("John A Smith", "John", "A Smith"),
    )
    def test_split_full_name(self, names):
        self.user.full_name = names[0]
        self.assertEqual(self.user.first_name, names[1])
        self.assertEqual(self.user.last_name, names[2])


@ddt
class UserCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.staff = self.fixture.staff
        self.url = factories.UserFactory.get_list_url()

    def test_staff_can_create_user(self):
        payload = dict(
            username="new_user",
            email="user@example.com",
            first_name="First",
            last_name="Last",
        )
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(uuid=response.data["uuid"])
        self.assertFalse(user.agreement_date)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="user_has_been_created_by_staff",
                message__icontains=user.username,
            ).exists()
        )

    @data(
        "global_support",
        "user",
    )
    def test_user_can_not_create_user(self, user):
        payload = dict(
            username="new_user",
            email="user@example.com",
            first_name="First",
            last_name="Last",
        )
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_change_password(self):
        url = factories.UserFactory.get_url(self.fixture.user, "change_password")
        old_password = self.fixture.user.password
        payload = dict(
            new_password="password",
        )
        self.client.force_authenticate(self.staff)
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.user.refresh_from_db()
        self.assertNotEqual(old_password, self.fixture.user.password)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="user_password_updated_by_staff",
                message__icontains=self.fixture.user.username,
            ).exists()
        )

    @data(
        "global_support",
        "user",
    )
    def test_user_can_not_change_password(self, user):
        url = factories.UserFactory.get_url(self.fixture.user, "change_password")
        payload = dict(
            new_password="password",
        )
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class UserPasswordManagementTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.staff = self.fixture.staff

    def test_staff_can_see_has_usable_password(self):
        url = factories.UserFactory.get_url(self.fixture.user)
        self.client.force_authenticate(self.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("has_usable_password", response.data)
        self.assertTrue(response.data["has_usable_password"])

    def test_support_cannot_see_has_usable_password_of_other_user(self):
        other_user = factories.UserFactory()
        url = factories.UserFactory.get_url(other_user)
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("has_usable_password", response.data)

    def test_user_can_see_has_usable_password_on_own_profile(self):
        url = factories.UserFactory.get_url(self.fixture.user)
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("has_usable_password", response.data)
        self.assertTrue(response.data["has_usable_password"])

    def test_has_usable_password_false_after_removal(self):
        self.fixture.user.set_unusable_password()
        self.fixture.user.save()

        url = factories.UserFactory.get_url(self.fixture.user)
        self.client.force_authenticate(self.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["has_usable_password"])

    def test_staff_can_remove_password(self):
        self.fixture.user.set_password("some_password")
        self.fixture.user.save()
        self.assertTrue(self.fixture.user.has_usable_password())

        url = factories.UserFactory.get_url(self.fixture.user, "remove_password")
        self.client.force_authenticate(self.staff)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.user.refresh_from_db()
        self.assertFalse(self.fixture.user.has_usable_password())
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="user_password_removed_by_staff",
                message__icontains=self.fixture.user.username,
            ).exists()
        )

    @data("global_support", "user")
    def test_non_staff_cannot_remove_password(self, user):
        url = factories.UserFactory.get_url(self.fixture.user, "remove_password")
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UserNotificationsEnabledTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.user = self.fixture.user
        self.staff = self.fixture.staff
        self.url = factories.UserFactory.get_url(self.user)

    def test_notifications_enabled_field_is_returned(self):
        """
        Notifications_enabled field is returned in the response.
        """
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("notifications_enabled", response.data)

    def test_non_staff_user_cannot_update_notifications_enabled(self):
        """
        Non-staff user cannot update notifications_enabled field.
        """
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            self.url, {"notifications_enabled": False, "agree_with_policy": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.notifications_enabled)

    def test_staff_user_can_update_notifications_enabled(self):
        """
        Staff user can update notifications_enabled field.
        """
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            self.url, {"notifications_enabled": False, "agree_with_policy": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.notifications_enabled)


class UserFilterIsStaffIsSupportTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.staff = self.fixture.staff
        self.regular_user = self.fixture.user
        self.support_user = factories.UserFactory(is_support=True)

        # Create some test users with different roles
        self.staff_user = factories.UserFactory(is_staff=True)
        self.regular_user2 = factories.UserFactory()
        self.support_user2 = factories.UserFactory(is_support=True)

        self.url = factories.UserFactory.get_list_url()

    def test_staff_can_filter_staff_users(self):
        """Staff user can filter staff users"""
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"is_staff": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        users = [user["uuid"] for user in response.data]
        self.assertIn(self.staff.uuid.hex, users)
        self.assertIn(self.staff_user.uuid.hex, users)
        self.assertNotIn(self.regular_user.uuid.hex, users)

    def test_staff_can_filter_support_users(self):
        """Staff user can filter support users"""
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"is_support": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        users = [user["uuid"] for user in response.data]
        self.assertIn(self.support_user.uuid.hex, users)
        self.assertIn(self.support_user2.uuid.hex, users)
        self.assertNotIn(self.regular_user.uuid.hex, users)

    def test_support_can_filter_staff_users(self):
        """Support user can filter staff users"""
        self.client.force_authenticate(self.support_user)
        response = self.client.get(self.url, {"is_staff": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        users = [user["uuid"] for user in response.data]
        self.assertIn(self.staff.uuid.hex, users)
        self.assertIn(self.staff_user.uuid.hex, users)
        self.assertNotIn(self.regular_user.uuid.hex, users)

    def test_support_can_filter_support_users(self):
        """Support user can filter support users"""
        self.client.force_authenticate(self.support_user)
        response = self.client.get(self.url, {"is_support": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        users = [user["uuid"] for user in response.data]
        self.assertIn(self.support_user.uuid.hex, users)
        self.assertIn(self.support_user2.uuid.hex, users)
        self.assertNotIn(self.regular_user.uuid.hex, users)

    def test_regular_user_cannot_filter_staff_users(self):
        """Regular user cannot filter staff users"""
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.url, {"is_staff": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_regular_user_cannot_filter_support_users(self):
        """Regular user cannot filter support users"""
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.url, {"is_support": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class UserPermissionsFieldTest(test.APITestCase):
    """Tests for the permissions field in user serializer.

    Fixes CSCS-1XR: N+1 query on /api/users/ endpoint.
    """

    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)

        # Create users with different permission levels
        self.owner = factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

        self.admin = factories.UserFactory()
        self.project.add_user(self.admin, ProjectRole.ADMIN)

        self.user_with_multiple_roles = factories.UserFactory()
        self.customer.add_user(self.user_with_multiple_roles, CustomerRole.SUPPORT)
        self.project.add_user(self.user_with_multiple_roles, ProjectRole.MANAGER)

    def test_user_detail_includes_permissions_field(self):
        """User detail response includes permissions field with role information."""
        self.client.force_authenticate(self.owner)
        url = factories.UserFactory.get_url(self.owner)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("permissions", response.data)
        self.assertEqual(len(response.data["permissions"]), 1)

        perm = response.data["permissions"][0]
        self.assertEqual(perm["role_name"], CustomerRole.OWNER.name)
        self.assertEqual(perm["scope_name"], self.customer.name)

    def test_user_with_multiple_roles_returns_all_permissions(self):
        """User with multiple roles returns all permissions in the response."""
        self.client.force_authenticate(self.user_with_multiple_roles)
        url = factories.UserFactory.get_url(self.user_with_multiple_roles)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("permissions", response.data)
        self.assertEqual(len(response.data["permissions"]), 2)

        role_names = {p["role_name"] for p in response.data["permissions"]}
        self.assertIn(CustomerRole.SUPPORT.name, role_names)
        self.assertIn(ProjectRole.MANAGER.name, role_names)

    def test_user_list_includes_permissions_for_each_user(self):
        """User list response includes permissions field for each user."""
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find our test users in the response
        users_by_uuid = {u["uuid"]: u for u in response.data}

        owner_data = users_by_uuid.get(self.owner.uuid.hex)
        self.assertIsNotNone(owner_data)
        self.assertIn("permissions", owner_data)
        self.assertEqual(len(owner_data["permissions"]), 1)

        multi_role_data = users_by_uuid.get(self.user_with_multiple_roles.uuid.hex)
        self.assertIsNotNone(multi_role_data)
        self.assertEqual(len(multi_role_data["permissions"]), 2)


class MeSlimPermissionsTest(test.APITestCase):
    """The /api/users/me endpoint returns a trimmed permissions projection
    (MePermissionSerializer / WAL-8015), dropping fields that are redundant or
    unused for the current user, while /api/users/ keeps the full projection.
    """

    # The complete whitelist the me endpoint may expose per role. Fields sourced
    # through a nullable relation (e.g. customer_uuid on a customer-scoped role,
    # where scope.customer does not exist) are dropped by DRF, so the actual
    # keys are always a subset of this set.
    SLIM_FIELDS = {
        "role_name",
        "role_uuid",
        "scope_type",
        "scope_uuid",
        "scope_name",
        "customer_uuid",
        "customer_name",
        "project_uuid",
        "resource_uuid",
        "expiration_time",
    }

    # Dropped fields that the full projection emits unconditionally (they do not
    # depend on a nullable relation), so they are reliable to assert on.
    ALWAYS_PRESENT_DROPPED_FIELDS = {
        "uuid",
        "user_uuid",
        "user_name",
        "user_slug",
        "created",
        "is_active",
        "revoke_reason",
        "role_description",
        "scope_is_removed",
    }

    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.owner = factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

    def get_me_permissions(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.UserFactory.get_list_url("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("permissions", response.data)
        return response.data["permissions"]

    def test_me_permissions_only_expose_whitelisted_fields(self):
        # A project role resolves the full slim whitelist (project.customer and
        # the method fields all yield keys), so this is the strictest case.
        user = factories.UserFactory()
        self.project.add_user(user, ProjectRole.MANAGER)

        permission = self.get_me_permissions(user)[0]
        self.assertLessEqual(set(permission.keys()), self.SLIM_FIELDS)
        for field in ("role_name", "scope_type", "scope_uuid", "scope_name"):
            self.assertIn(field, permission)
        # customer_uuid resolves for a project scope (via scope.customer).
        self.assertEqual(permission["customer_uuid"], self.customer.uuid.hex)

    def test_me_permissions_omit_dropped_fields(self):
        permission = self.get_me_permissions(self.owner)[0]
        for field in self.ALWAYS_PRESENT_DROPPED_FIELDS:
            self.assertNotIn(field, permission)

    def test_me_permissions_retain_role_and_scope_data(self):
        permission = self.get_me_permissions(self.owner)[0]
        self.assertEqual(permission["role_name"], CustomerRole.OWNER.name)
        self.assertEqual(permission["scope_type"], "customer")
        self.assertEqual(permission["scope_uuid"], self.customer.uuid.hex)
        self.assertEqual(permission["scope_name"], self.customer.name)

    def test_me_returns_all_roles(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.SUPPORT)
        self.project.add_user(user, ProjectRole.MANAGER)

        permissions = self.get_me_permissions(user)
        self.assertEqual(len(permissions), 2)
        role_names = {p["role_name"] for p in permissions}
        self.assertEqual(
            role_names, {CustomerRole.SUPPORT.name, ProjectRole.MANAGER.name}
        )

    def test_user_detail_keeps_full_permission_fields(self):
        """Regression guard: /api/users/{uuid}/ still uses the full projection."""
        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.UserFactory.get_url(self.owner))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permission = response.data["permissions"][0]
        for field in self.ALWAYS_PRESENT_DROPPED_FIELDS:
            self.assertIn(field, permission)


class UserIdentityBridgeFieldsVisibilityTest(test.APITestCase):
    """Identity Bridge fields (is_identity_manager, managed_isds, active_isds)
    are visible read-only on own profile but hidden when viewing other users."""

    SELF_VISIBLE_FIELDS = ("is_identity_manager", "managed_isds", "active_isds")

    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.identity_manager = factories.UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:efp"],
            active_isds=["isd:efp"],
        )
        self.regular_user = factories.UserFactory()
        # Put both non-staff users in the same customer so they can see each other
        self.customer = factories.CustomerFactory()
        self.customer.add_user(self.identity_manager, CustomerRole.OWNER)
        self.customer.add_user(self.regular_user, CustomerRole.SUPPORT)

    def test_user_can_see_own_identity_bridge_fields(self):
        self.client.force_authenticate(self.identity_manager)
        response = self.client.get(factories.UserFactory.get_url(self.identity_manager))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.SELF_VISIBLE_FIELDS:
            self.assertIn(
                field, response.data, f"{field} should be visible on own profile"
            )
        self.assertTrue(response.data["is_identity_manager"])
        self.assertEqual(response.data["managed_isds"], ["isd:efp"])

    def test_user_cannot_see_identity_bridge_fields_of_other_user(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(factories.UserFactory.get_url(self.identity_manager))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.SELF_VISIBLE_FIELDS:
            self.assertNotIn(
                field, response.data, f"{field} should be hidden for other users"
            )

    def test_staff_can_see_identity_bridge_fields_of_any_user(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(factories.UserFactory.get_url(self.identity_manager))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.SELF_VISIBLE_FIELDS:
            self.assertIn(field, response.data, f"{field} should be visible to staff")

    def test_identity_bridge_fields_are_read_only_on_own_profile(self):
        self.client.force_authenticate(self.identity_manager)
        self.client.patch(
            factories.UserFactory.get_url(self.identity_manager),
            {"managed_isds": ["isd:fenix"]},
            format="json",
        )
        self.identity_manager.refresh_from_db()
        self.assertEqual(self.identity_manager.managed_isds, ["isd:efp"])

    def test_regular_user_sees_own_identity_bridge_fields_with_defaults(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(factories.UserFactory.get_url(self.regular_user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("is_identity_manager", response.data)
        self.assertFalse(response.data["is_identity_manager"])


class UserAggregationEndpointsTest(test.APITestCase):
    """Tests for user aggregation endpoints (staff/support only)."""

    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True, agreement_date=timezone.now())
        self.support = factories.UserFactory(
            is_support=True, agreement_date=timezone.now()
        )
        self.regular_user = factories.UserFactory(agreement_date=timezone.now())

        # Create users with different statuses and languages
        self.active_users = [
            factories.UserFactory(is_active=True, preferred_language="en"),
            factories.UserFactory(is_active=True, preferred_language="en"),
            factories.UserFactory(is_active=True, preferred_language="de"),
            factories.UserFactory(is_active=True, preferred_language=""),
        ]
        self.inactive_users = [
            factories.UserFactory(is_active=False),
            factories.UserFactory(is_active=False),
        ]

    def test_anonymous_cannot_access_user_active_status_count(self):
        url = factories.UserFactory.get_list_url("user_active_status_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_access_user_active_status_count(self):
        self.client.force_authenticate(self.regular_user)
        url = factories.UserFactory.get_list_url("user_active_status_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_access_user_active_status_count(self):
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_active_status_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        status_counts = {item["status"]: item["count"] for item in response.data}
        self.assertIn("active", status_counts)
        self.assertIn("inactive", status_counts)
        # 3 setup users (staff, support, regular) + 4 active users = 7
        self.assertEqual(status_counts["active"], 7)
        self.assertEqual(status_counts["inactive"], 2)

    def test_support_can_access_user_active_status_count(self):
        self.client.force_authenticate(self.support)
        url = factories.UserFactory.get_list_url("user_active_status_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_anonymous_cannot_access_user_language_count(self):
        url = factories.UserFactory.get_list_url("user_language_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_access_user_language_count(self):
        self.client.force_authenticate(self.regular_user)
        url = factories.UserFactory.get_list_url("user_language_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_access_user_language_count(self):
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_language_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that response contains expected languages
        language_counts = {item["language"]: item["count"] for item in response.data}
        self.assertIn("en", language_counts)
        self.assertIn("de", language_counts)
        self.assertEqual(language_counts["en"], 2)
        self.assertEqual(language_counts["de"], 1)

    def test_support_can_access_user_language_count(self):
        self.client.force_authenticate(self.support)
        url = factories.UserFactory.get_list_url("user_language_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_empty_language_is_reported_as_unset(self):
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_language_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        language_counts = {item["language"]: item["count"] for item in response.data}
        self.assertIn("unset", language_counts)

    def test_anonymous_cannot_access_user_registration_trend(self):
        url = factories.UserFactory.get_list_url("user_registration_trend")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_access_user_registration_trend(self):
        self.client.force_authenticate(self.regular_user)
        url = factories.UserFactory.get_list_url("user_registration_trend")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_access_user_registration_trend(self):
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_registration_trend")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # All users created in setUp should be in the same month
        self.assertGreater(len(response.data), 0)
        for item in response.data:
            self.assertIn("month", item)
            self.assertIn("count", item)
            # Month format should be YYYY-MM
            self.assertRegex(item["month"], r"^\d{4}-\d{2}$")

    def test_support_can_access_user_registration_trend(self):
        self.client.force_authenticate(self.support)
        url = factories.UserFactory.get_list_url("user_registration_trend")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_language_count_only_includes_active_users(self):
        """Language count should only count active users."""
        # Create an inactive user with a unique language
        factories.UserFactory(is_active=False, preferred_language="fr")

        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_language_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        language_counts = {item["language"]: item["count"] for item in response.data}
        # French should not appear since the user is inactive
        self.assertNotIn("fr", language_counts)

    def test_active_status_count_includes_all_users(self):
        """Active status count should include both active and inactive users."""
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_active_status_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        status_counts = {item["status"]: item["count"] for item in response.data}
        total = status_counts["active"] + status_counts["inactive"]
        # Total should be all users created in setUp
        self.assertEqual(total, 9)

    def test_registration_trend_includes_all_users(self):
        """Registration trend should include both active and inactive users."""
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_registration_trend")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        total_count = sum(item["count"] for item in response.data)
        # Should include all 9 users (3 setup + 4 active + 2 inactive)
        self.assertEqual(total_count, 9)

    def test_registration_trend_is_ordered_by_month(self):
        """Registration trend should be ordered by month ascending."""
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_registration_trend")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        months = [item["month"] for item in response.data]
        self.assertEqual(months, sorted(months))

    def test_language_count_is_ordered_by_count_descending(self):
        """Language count should be ordered by count descending."""
        self.client.force_authenticate(self.staff)
        url = factories.UserFactory.get_list_url("user_language_count")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        counts = [item["count"] for item in response.data]
        self.assertEqual(counts, sorted(counts, reverse=True))


class ProfileCompletenessTest(test.APITestCase):
    """Test profile completeness endpoint and /me endpoint update."""

    def setUp(self):
        self.user = factories.UserFactory(
            agreement_date=timezone.now(),
            phone_number="",
            organization="",
        )

    def test_anonymous_cannot_access_profile_completeness(self):
        url = factories.UserFactory.get_list_url("profile_completeness")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @mock.patch("waldur_core.core.user_attributes.config")
    def test_profile_completeness_returns_correct_structure(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number", "organization"]
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = True

        self.client.force_authenticate(self.user)
        url = factories.UserFactory.get_list_url("profile_completeness")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("is_complete", response.data)
        self.assertIn("missing_fields", response.data)
        self.assertIn("mandatory_fields", response.data)
        self.assertIn("enforcement_enabled", response.data)

    @mock.patch("waldur_core.core.user_attributes.config")
    def test_profile_completeness_shows_missing_fields(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = False

        self.client.force_authenticate(self.user)
        url = factories.UserFactory.get_list_url("profile_completeness")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_complete"])
        self.assertIn("phone_number", response.data["missing_fields"])

    @mock.patch("waldur_core.core.user_attributes.config")
    def test_profile_completeness_shows_complete_when_filled(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = False

        self.user.phone_number = "+1234567890"
        self.user.save()

        self.client.force_authenticate(self.user)
        url = factories.UserFactory.get_list_url("profile_completeness")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_complete"])
        self.assertEqual(response.data["missing_fields"], [])

    @mock.patch("waldur_core.core.user_attributes.config")
    def test_me_endpoint_includes_profile_completeness(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = True

        self.client.force_authenticate(self.user)
        url = factories.UserFactory.get_list_url("me")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("profile_completeness", response.data)
        self.assertIn("is_complete", response.data["profile_completeness"])
        self.assertIn("missing_fields", response.data["profile_completeness"])
        self.assertIn("mandatory_fields", response.data["profile_completeness"])
        self.assertIn("enforcement_enabled", response.data["profile_completeness"])

    @mock.patch("waldur_core.core.user_attributes.config")
    def test_profile_completeness_shows_enforcement_status(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = []
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = True

        self.client.force_authenticate(self.user)
        url = factories.UserFactory.get_list_url("profile_completeness")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["enforcement_enabled"])


class GenderFieldTest(test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory(agreement_date=timezone.now())
        self.client.force_authenticate(self.user)
        self.url = factories.UserFactory.get_url(self.user)

    def test_patch_gender(self):
        response = self.client.patch(self.url, {"gender": "female"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.gender, "female")
        self.assertEqual(response.data["gender"], "female")

    def test_patch_gender_with_invalid_value(self):
        response = self.client.patch(self.url, {"gender": "invalid"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.gender, None)
        self.assertIn("is not a valid choice", str(response.data["gender"]))

    def test_get_gender(self):
        self.user.gender = "male"
        self.user.save()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["gender"], "male")


class UserShouldProtectUserDetailsFieldTest(test.APITestCase):
    """Read-only `should_protect_user_details` field reflects the model property."""

    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True, agreement_date=timezone.now())

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_field_true_for_protected_registration_method(self):
        target = factories.UserFactory(registration_method="PROTECTED")
        self.client.force_authenticate(self.staff)
        response = self.client.get(factories.UserFactory.get_url(target))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["should_protect_user_details"])

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_field_false_for_unprotected_registration_method(self):
        target = factories.UserFactory(registration_method="LOCAL")
        self.client.force_authenticate(self.staff)
        response = self.client.get(factories.UserFactory.get_url(target))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["should_protect_user_details"])

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_field_is_read_only(self):
        target = factories.UserFactory(registration_method="LOCAL")
        self.client.force_authenticate(self.staff)
        # Even staff cannot toggle this field — it's computed from settings.
        self.client.patch(
            factories.UserFactory.get_url(target),
            {"should_protect_user_details": True},
            format="json",
        )
        target.refresh_from_db()
        self.assertFalse(target.should_protect_user_details)


class UserOrganizationVatCodeTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.user = self.fixture.user
        self.user.agreement_date = timezone.now()
        self.user.save()
        self.client.force_authenticate(self.user)
        self.url = factories.UserFactory.get_url(self.user)

    def test_valid_vat_code_accepted(self):
        response = self.client.patch(
            self.url, {"organization_vat_code": "DE123456789"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.organization_vat_code, "DE123456789")

    def test_invalid_vat_code_rejected(self):
        response = self.client.patch(
            self.url, {"organization_vat_code": "invalid"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("organization_vat_code", response.data)

    def test_blank_vat_code_accepted(self):
        response = self.client.patch(
            self.url, {"organization_vat_code": ""}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.organization_vat_code, "")

    def test_field_visible_in_user_detail(self):
        self.user.organization_vat_code = "FI12345678"
        self.user.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["organization_vat_code"], "FI12345678")


class UserOrganizationAddressTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.user = self.fixture.user
        self.user.agreement_date = timezone.now()
        self.user.save()
        self.client.force_authenticate(self.user)
        self.url = factories.UserFactory.get_url(self.user)

    def test_address_accepted(self):
        response = self.client.patch(
            self.url,
            {"organization_address": "123 Main St, Helsinki"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.organization_address, "123 Main St, Helsinki")

    def test_blank_address_accepted(self):
        response = self.client.patch(
            self.url, {"organization_address": ""}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.organization_address, "")

    def test_field_visible_in_user_detail(self):
        self.user.organization_address = "456 University Ave"
        self.user.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["organization_address"], "456 University Ave")
