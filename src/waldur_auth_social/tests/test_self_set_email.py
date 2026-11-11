"""Single sign-on does not find an account by an email its user set.

A self-service email change needs only the API token and a mailbox the
requester controls. Were the account found by that address, whoever held the
token could sign in to it at an identity provider as that address, for good.
Such a sign-in is refused, rather than given a new account: someone who changed
their own email and now signs in through an identity provider asks support.
"""

from unittest import mock

import responses
from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_auth_social.tests.oidc_helpers import mock_token_endpoint
from waldur_auth_social.tests.test_oidc import assert_login_failed_redirect
from waldur_auth_social.utils import (
    create_or_update_oauth_user,
    update_user_attributes_from_source,
)
from waldur_auth_social.views import OIDC_STATE_KEY
from waldur_core.configuration.testing import override_config
from waldur_core.core import reauthentication
from waldur_core.core.enums import ReauthenticationAction, ReauthenticationMethod
from waldur_core.core.models import ChangeEmailRequest, User
from waldur_core.structure import views
from waldur_core.structure.tests import factories as structure_factories

ADDRESS = "elsewhere@example.com"


class SelfSetEmailSignInTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.url = reverse(f"auth_{self.provider.provider}_complete")
        session = self.client.session
        session[OIDC_STATE_KEY] = "state"
        session.save()
        responses.start()
        self.addCleanup(responses.reset)
        self.addCleanup(responses.stop)

        self.user = structure_factories.UserFactory(
            username="account-holder",
            email="holder@example.com",
            agreement_date=timezone.now(),
        )

    def change_email_grant(self, user):
        # A change of email asks the person present to re-authenticate.
        _, secret = reauthentication.issue_grant(
            user, ReauthenticationAction.CHANGE_EMAIL, ReauthenticationMethod.PASSWORD
        )
        return {"HTTP_X_REAUTHENTICATION_GRANT": secret}

    def sign_in(self, email, sub="someone-at-the-provider"):
        mock_token_endpoint(self.provider, lambda: sub)
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json={"sub": sub, "given_name": "S", "family_name": "O", "email": email},
            status=status.HTTP_200_OK,
        )
        return self.client.get(self.url, {"state": "state", "code": "code"})

    def set_email_as_the_user(self, email):
        # Through the API, as the user, re-authenticated: the address is
        # still one its user chose, which must not become a sign-in identifier.
        token, _ = Token.objects.get_or_create(user=self.user)
        api = test.APIClient()
        api.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        response = api.post(
            structure_factories.UserFactory.get_url(self.user, "change_email"),
            {"email": email},
            format="json",
            **self.change_email_grant(self.user),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        code = ChangeEmailRequest.objects.get(user=self.user).uuid.hex
        response = test.APIClient().post(
            "/api/users/confirm_email/", {"code": code}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, email)

    def assertSignInRefused(self, response):
        assert_login_failed_redirect(
            self,
            response,
            "This sign-in cannot be completed for this email address. "
            "Please contact support.",
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "account-holder")
        self.assertEqual(User.objects.filter(email__iexact=ADDRESS).count(), 1)

    def set_email_as_staff(self, email):
        staff_user = structure_factories.UserFactory(is_staff=True)
        staff = test.APIClient()
        staff.force_authenticate(staff_user)
        response = staff.patch(
            structure_factories.UserFactory.get_url(self.user),
            {"email": email},
            format="json",
            **self.change_email_grant(staff_user),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_matching_by_email_does_not_find_it(self):
        self.set_email_as_the_user(ADDRESS)

        self.assertSignInRefused(self.sign_in(ADDRESS))

    def test_a_provider_that_identifies_users_by_email_does_not_find_it(self):
        self.provider.user_field = "email"
        self.provider.user_claim = "email"
        self.provider.save()
        self.set_email_as_the_user(ADDRESS)

        self.assertSignInRefused(self.sign_in(ADDRESS))

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_an_email_staff_set_is_found_again(self):
        # Support's way out for someone refused above: staff set the address.
        # Away and back: the record names who set it last, not the address.
        self.set_email_as_the_user(ADDRESS)
        self.set_email_as_staff("interim@example.com")
        self.set_email_as_staff(ADDRESS)

        response = self.sign_in(ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "someone-at-the-provider")

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_an_account_whose_email_nobody_changed_is_found(self):
        response = self.sign_in("holder@example.com")

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "someone-at-the-provider")

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_background_synchronisation_is_not_refused(self):
        # No sign-in: an identity pulled for provisioning gets an account of its
        # own, as before, rather than the self-set one.
        self.set_email_as_the_user(ADDRESS)

        user, _ = create_or_update_oauth_user(
            self.provider,
            {"sub": "pulled", "given_name": "P", "family_name": "U", "email": ADDRESS},
            is_interactive_login=False,
        )

        self.assertNotEqual(user.pk, self.user.pk)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "account-holder")

    def test_a_stale_copy_does_not_drop_the_mark(self):
        # A sync holding a copy read before the change pushes the email it
        # knows; the address the user set must not become a way in.
        stale = User.objects.get(pk=self.user.pk)
        self.set_email_as_the_user(ADDRESS)

        update_user_attributes_from_source(
            stale, {"email": "holder@example.com"}, source="isd:directory"
        )

        self.assertNotIn(
            self.user.pk, [user.pk for user in User.find_by_email(ADDRESS)]
        )

    def test_a_confirmation_does_not_override_staff_who_replaced_the_request(self):
        token, _ = Token.objects.get_or_create(user=self.user)
        api = test.APIClient()
        api.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        api.post(
            structure_factories.UserFactory.get_url(self.user, "change_email"),
            {"email": ADDRESS},
            format="json",
            **self.change_email_grant(self.user),
        )
        code = ChangeEmailRequest.objects.get(user=self.user).uuid.hex
        fetch = views.get_object_or_404

        def fetched_then_replaced_by_staff(*args, **kwargs):
            change_request = fetch(*args, **kwargs)
            ChangeEmailRequest.objects.filter(pk=change_request.pk).delete()
            return change_request

        with mock.patch.object(
            views, "get_object_or_404", side_effect=fetched_then_replaced_by_staff
        ):
            response = test.APIClient().post(
                "/api/users/confirm_email/", {"code": code}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "holder@example.com")
