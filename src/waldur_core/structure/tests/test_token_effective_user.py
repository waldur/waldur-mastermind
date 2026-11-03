"""A personal access token without the staff or support scope reads as the
user without that role (#987).

The request's user is a ``TokenScopedUser`` whose ``is_staff`` / ``is_support``
follow the token, so the list and detail visibility decided from those flags
follows it too. The account's own flags must never change.
"""

from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_save
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.test import APIRequestFactory

from waldur_core.configuration.testing import override_config
from waldur_core.core.authentication import PATAuthentication
from waldur_core.core.models import TokenScopedUser, User
from waldur_core.core.permissions import (
    TokenCarriesUserRole,
    has_staff_access,
    has_staff_or_support_read_access,
    has_support_access,
)
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.permissions.utils import (
    check_pat_staff_scope,
    check_pat_support_scope,
)
from waldur_core.structure.tests import factories
from waldur_mastermind.invoices.tests import factories as invoice_factories

NARROW = [PermissionEnum.LIST_ORDERS.value]
STAFF = [PermissionEnum.STAFF_ACCESS.value]
SUPPORT = [PermissionEnum.SUPPORT_ACCESS.value]


def token_user(**flags):
    user = factories.UserFactory(can_use_personal_access_tokens=True, **flags)
    Token.objects.get_or_create(user=user)
    return user


def authenticate(user, scopes):
    """Authenticate a request with a new token of ``user`` as REST does."""
    pat = _create_pat(user, scopes=scopes, bindings=[])
    request = APIRequestFactory().get(
        "/api/customers/", HTTP_AUTHORIZATION=_auth_header(pat)
    )
    return PATAuthentication().authenticate(request)


@override_config(PAT_ENABLED=True)
class CustomerVisibilityTest(test.APITestCase):
    def setUp(self):
        self.staff = token_user(is_staff=True)
        self.support = token_user(is_support=True)
        self.foreign = factories.CustomerFactory(name="foreign")
        # The control: an organization the user does hold a role in stays
        # visible to a narrow token, so a 404 is not just a broken token.
        self.own = factories.CustomerFactory(name="own")
        self.own.add_user(self.staff, CustomerRole.OWNER)
        self.own.add_user(self.support, CustomerRole.OWNER)

    def use_token(self, user, scopes):
        pat = _create_pat(user, scopes=scopes, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))

    def listed(self):
        response = self.client.get(factories.CustomerFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def detail(self, customer):
        return self.client.get(factories.CustomerFactory.get_url(customer))

    def assert_sees_only_own(self):
        self.assertEqual(
            self.detail(self.foreign).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(self.detail(self.own).status_code, status.HTTP_200_OK)
        listed = self.listed()
        self.assertNotIn(self.foreign.uuid.hex, listed)
        self.assertIn(self.own.uuid.hex, listed)

    def assert_sees_all(self):
        self.assertEqual(self.detail(self.foreign).status_code, status.HTTP_200_OK)
        self.assertIn(self.foreign.uuid.hex, self.listed())

    def test_staff_token_without_staff_scope_sees_only_its_organizations(self):
        self.use_token(self.staff, NARROW)
        self.assert_sees_only_own()

    def test_staff_token_with_staff_scope_sees_every_organization(self):
        self.use_token(self.staff, STAFF)
        self.assert_sees_all()

    def test_staff_token_with_support_scope_reads_as_support(self):
        self.use_token(self.staff, SUPPORT)
        self.assert_sees_all()

    def test_staff_session_sees_every_organization(self):
        self.client.force_authenticate(self.staff)
        self.assert_sees_all()

    def test_support_token_without_support_scope_sees_only_its_organizations(self):
        self.use_token(self.support, NARROW)
        self.assert_sees_only_own()

    def test_support_token_with_support_scope_sees_every_organization(self):
        self.use_token(self.support, SUPPORT)
        self.assert_sees_all()

    def test_support_token_with_staff_scope_sees_every_organization(self):
        self.use_token(self.support, STAFF)
        self.assert_sees_all()

    def test_support_session_sees_every_organization(self):
        self.client.force_authenticate(self.support)
        self.assert_sees_all()


@override_config(PAT_ENABLED=True)
class InvoiceVisibilityTest(test.APITestCase):
    """A list from another app keyed on filter_queryset_for_user."""

    def setUp(self):
        self.staff = token_user(is_staff=True)
        self.foreign = invoice_factories.InvoiceFactory()
        self.own = invoice_factories.InvoiceFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVOICES)
        self.own.customer.add_user(self.staff, CustomerRole.OWNER)

    def listed(self, scopes):
        pat = _create_pat(self.staff, scopes=scopes, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get(invoice_factories.InvoiceFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def test_token_without_staff_scope_lists_only_its_invoices(self):
        listed = self.listed(NARROW)
        self.assertIn(self.own.uuid.hex, listed)
        self.assertNotIn(self.foreign.uuid.hex, listed)

    def test_token_with_staff_scope_lists_every_invoice(self):
        listed = self.listed(STAFF)
        self.assertIn(self.own.uuid.hex, listed)
        self.assertIn(self.foreign.uuid.hex, listed)


@override_config(PAT_ENABLED=True)
class CurrentUserTest(test.APITestCase):
    def me(self, user, scopes):
        pat = _create_pat(user, scopes=scopes, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/users/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Fields hidden from a regular user read as None.
        return response.data.get("is_staff"), response.data.get("is_support")

    def test_reports_the_flags_the_token_acts_with(self):
        staff = token_user(is_staff=True)
        support = token_user(is_support=True)
        # As for any regular user, the flags are left out.
        self.assertEqual(self.me(staff, NARROW), (None, None))
        self.assertEqual(self.me(staff, SUPPORT), (False, True))
        self.assertEqual(self.me(staff, STAFF), (True, False))
        self.assertEqual(self.me(support, NARROW), (None, None))
        self.assertEqual(self.me(support, SUPPORT), (False, True))

    def test_own_profile_stays_visible(self):
        staff = token_user(is_staff=True)
        other_staff = token_user(is_staff=True)
        pat = _create_pat(staff, scopes=NARROW, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        own = self.client.get(factories.UserFactory.get_url(staff))
        self.assertEqual(own.status_code, status.HTTP_200_OK)
        other = self.client.get(factories.UserFactory.get_url(other_staff))
        self.assertEqual(other.status_code, status.HTTP_404_NOT_FOUND)


@override_config(PAT_ENABLED=True)
class AccountFlagsPersistenceTest(test.APITestCase):
    """The narrowed flags never reach the database."""

    def setUp(self):
        self.staff = token_user(is_staff=True, is_support=True)
        self.user, _ = authenticate(self.staff, NARROW)

    def assert_account_unchanged(self):
        stored = User.objects.get(pk=self.staff.pk)
        self.assertTrue(stored.is_staff)
        self.assertTrue(stored.is_support)

    def test_request_user_is_narrowed(self):
        self.assertIsInstance(self.user, TokenScopedUser)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_support)
        self.assertTrue(self.user.account_is_staff)
        self.assertTrue(self.user.account_is_support)

    def test_full_save_keeps_the_account_flags(self):
        self.user.job_title = "Narrowed"
        self.user.save()
        self.assert_account_unchanged()
        self.assertEqual(User.objects.get(pk=self.staff.pk).job_title, "Narrowed")
        # Still narrowed after the save.
        self.assertFalse(self.user.is_staff)

    def test_save_of_the_flags_writes_nothing(self):
        self.user.save(update_fields=["is_staff", "is_support"])
        self.user.save(update_fields=["is_staff", "job_title"])
        self.assert_account_unchanged()

    def test_flags_cannot_be_assigned(self):
        with self.assertRaises(AttributeError):
            self.user.is_staff = self.user.is_staff
        with self.assertRaises(AttributeError):
            self.user.is_support = False
        self.user.save()
        self.assert_account_unchanged()

    def test_refresh_from_db_keeps_the_narrowing(self):
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertTrue(self.user.account_is_staff)
        self.user.refresh_from_db(fields=["is_staff"])
        self.assertFalse(self.user.is_staff)
        self.user.save()
        self.assert_account_unchanged()

    def test_receivers_see_a_user_with_the_account_flags(self):
        seen = []

        def receiver(sender, instance, **kwargs):
            seen.append(
                (
                    sender,
                    instance.is_staff,
                    instance.tracker.has_changed("is_staff"),
                )
            )

        post_save.connect(receiver, sender=User)
        try:
            self.user.save(update_fields=["job_title"])
        finally:
            post_save.disconnect(receiver, sender=User)
        self.assertEqual(seen, [(User, True, False)])

    def test_self_update_through_the_api_keeps_the_account_flags(self):
        pat = _create_pat(self.staff, scopes=NARROW, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.patch(
            factories.UserFactory.get_url(self.staff),
            {"job_title": "Engineer", "is_staff": False, "is_support": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        stored = User.objects.get(pk=self.staff.pk)
        self.assertEqual(stored.job_title, "Engineer")
        self.assert_account_unchanged()


@override_config(PAT_ENABLED=True)
class IdentityTest(test.APITestCase):
    def test_narrowed_user_is_the_same_user(self):
        staff = token_user(is_staff=True)
        user, pat = authenticate(staff, NARROW)
        stored = User.objects.get(pk=staff.pk)

        self.assertIsInstance(user, User)
        self.assertIs(pat.user, user)
        self.assertEqual(user, stored)
        self.assertEqual(stored, user)
        self.assertEqual(hash(user), hash(stored))
        self.assertEqual((user.pk, user.uuid), (stored.pk, stored.uuid))
        self.assertIn(user, User.objects.all())
        self.assertEqual(list(User.objects.filter(pk=user.pk)), [stored])
        self.assertEqual(
            list(Token.objects.filter(user=user)), [Token.objects.get(user=staff)]
        )

        self.assertEqual(
            ContentType.objects.get_for_model(user),
            ContentType.objects.get_for_model(User),
        )

        customer = factories.CustomerFactory()
        customer.add_user(user, CustomerRole.OWNER, created_by=user)
        self.assertTrue(customer.has_user(stored, CustomerRole.OWNER))

    def test_regular_and_fully_scoped_tokens_keep_a_plain_user(self):
        for flags, scopes in (
            ({}, NARROW),
            ({"is_staff": True}, STAFF),
            ({"is_support": True}, SUPPORT),
            ({"is_support": True}, STAFF),
        ):
            with self.subTest(flags=flags, scopes=scopes):
                user, _ = authenticate(token_user(**flags), scopes)
                self.assertIs(type(user), User)


@override_config(PAT_ENABLED=True)
class HelperAnswersTest(test.APITestCase):
    """The request-aware helpers answer as they did before the narrowing."""

    def request_for(self, user, scopes):
        user, pat = authenticate(user, scopes)
        request = APIRequestFactory().get("/")
        request.user = user
        request.auth = pat
        return request

    def answers(self, request):
        return {
            "staff": has_staff_access(request),
            "support": has_support_access(request),
            "read_all": has_staff_or_support_read_access(request),
            "staff_scope": check_pat_staff_scope(request),
            "support_scope": check_pat_support_scope(request),
            "carries_role": TokenCarriesUserRole().has_permission(request, None),
        }

    def test_answers(self):
        cases = [
            # (account flags, token scopes, staff, support, read_all, carries)
            ({"is_staff": True}, NARROW, False, False, False, False),
            ({"is_staff": True}, SUPPORT, False, True, True, False),
            ({"is_staff": True}, STAFF, True, True, True, True),
            ({"is_support": True}, NARROW, False, False, False, False),
            ({"is_support": True}, SUPPORT, False, True, True, True),
            ({"is_support": True}, STAFF, False, True, True, True),
            ({}, NARROW, False, False, False, True),
        ]
        for flags, scopes, staff, support, read_all, carries in cases:
            with self.subTest(flags=flags, scopes=scopes):
                answers = self.answers(self.request_for(token_user(**flags), scopes))
                self.assertEqual(answers["staff"], staff)
                self.assertEqual(answers["support"], support)
                self.assertEqual(answers["read_all"], read_all)
                self.assertEqual(answers["carries_role"], carries)
                self.assertEqual(
                    answers["staff_scope"], PermissionEnum.STAFF_ACCESS.value in scopes
                )
