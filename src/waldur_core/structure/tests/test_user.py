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


class UserPermissionApiTest(test.APITransactionTestCase):
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


class UserPermissionApiListTest(test.APITransactionTestCase):
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


class UserFilterTest(test.APITransactionTestCase):
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


class CustomUsersFilterTest(test.APITransactionTestCase):
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
class UserUpdateTest(test.APITransactionTestCase):
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


@mock.patch("waldur_core.structure.handlers.event_logger")
class NotificationsProfileChangesTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = factories.UserFactory(full_name="John", email="john@example.org")

    def test_sent_notification_if_change_owner_email(self, mock_event_logger):
        customer = factories.CustomerFactory()
        customer.add_user(self.user, CustomerRole.OWNER)
        old_email = self.user.email
        new_email = "new_email_" + old_email
        self.user.email = new_email
        self.user.save()
        self.assertEqual(mock_event_logger.user.info.call_count, 1)

    def test_notification_message(self, mock_event_logger):
        customer = factories.CustomerFactory(name="Customer", abbreviation="ABC")
        customer.add_user(self.user, CustomerRole.OWNER)
        old_email = self.user.email
        new_email = "new_email_" + old_email
        self.user.email = new_email
        self.user.save()
        msg = mock_event_logger.user.info.call_args[0][0]
        test_msg = (
            f"User John (id={self.user.id}) "
            "profile has been updated: email from john@example.org to new_email_john@example.org."
        )
        self.assertEqual(test_msg, msg)

    def test_dont_sent_notification_if_change_owner_other_field(
        self, mock_event_logger
    ):
        customer = factories.CustomerFactory()
        customer.add_user(self.user, CustomerRole.OWNER)
        token_lifetime = 100 + self.user.token_lifetime
        self.user.token_lifetime = token_lifetime
        self.user.save()
        self.assertEqual(mock_event_logger.user.info.call_count, 0)

    def test_dont_sent_notification_if_change_not_owner_email(self, mock_event_logger):
        new_email = "new_email_" + self.user.email
        self.user.email = new_email
        self.user.save()
        self.assertEqual(mock_event_logger.user.info.call_count, 0)


@ddt
class UserFullnameTest(test.APITransactionTestCase):
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
class UserCreateTest(test.APITransactionTestCase):
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


class UserPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.user1 = factories.UserFactory()
        self.user2 = factories.UserFactory()
        self.user3 = factories.UserFactory()
        self.user4 = factories.UserFactory()

        self.customer1 = factories.CustomerFactory()
        self.customer1.add_user(self.user1, CustomerRole.OWNER)
        self.customer1.add_user(self.user2, CustomerRole.SUPPORT)

        self.customer2 = factories.CustomerFactory()
        self.customer2.add_user(self.user3, CustomerRole.OWNER)

        self.url = "http://testserver/api/user-permissions/"

    def test_user_can_get_self_and_connected_users_permissions(self):
        self.client.force_authenticate(self.user1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data[0]["user_uuid"], self.user1.uuid)
        self.assertEqual(response.data[1]["user_uuid"], self.user2.uuid)

        self.client.force_authenticate(self.user3)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["user_uuid"], self.user3.uuid)

    def test_user_can_not_get_not_connected_users_permissions(self):
        self.client.force_authenticate(self.user4)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)
