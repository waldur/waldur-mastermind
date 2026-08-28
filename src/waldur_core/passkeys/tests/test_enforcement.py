"""One test per bypass named in the issue.

Each of these was a way to obtain a privileged session without satisfying a
passkey. Together they are the difference between enforcement being a property
of the credential and being a property of the login page.
"""

from django.contrib.auth import get_user_model
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.authentication import can_access_admin_site
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.passkeys.models import (
    PasskeyVerifiedSession,
    is_session_verified,
    mark_session_verified,
)
from waldur_core.passkeys.tests.factories import PasskeyCredentialFactory
from waldur_core.passkeys.tests.helpers import ORIGIN, RP_ID
from waldur_core.structure.tests import factories as structure_factories

User = get_user_model()


def enforce(**kwargs):
    settings = dict(
        AUTHENTICATION_METHODS=["LOCAL_SIGNIN", "PASSKEY_SIGNIN", "PASSKEY_MFA"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_ALLOWED_ORIGINS=[ORIGIN],
        PASSKEY_ENFORCED_FOR_STAFF=True,
    )
    settings.update(kwargs)
    return override_waldur_core_settings(**settings)


@enforce()
class StaffTokenExposureTest(test.APITestCase):
    """A staff password must not yield another user's credential.

    While this endpoint returned the raw key, one compromised staff password
    was a durable, passkey-free session as anybody in the deployment — so no
    second factor could mean anything.
    """

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.victim = structure_factories.UserFactory()
        self.client.force_authenticate(self.staff)

    def test_staff_cannot_read_another_users_raw_token(self):
        response = self.client.get(
            f"/api/users/{self.victim.uuid.hex}/token/",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("token", response.data)

    def test_refresh_token_does_not_return_the_new_key(self):
        response = self.client.post(
            f"/api/users/{self.victim.uuid.hex}/refresh_token/",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("token", response.data)

    def test_the_endpoint_still_answers_what_it_is_for(self):
        """Removing the key must not remove the operational value."""
        response = self.client.get(f"/api/users/{self.victim.uuid.hex}/token/")

        self.assertIn("created", response.data)
        self.assertEqual(response.data["user_username"], self.victim.username)


class OwnTokenTest(test.APITestCase):
    """The user's own token is theirs, and stays visible."""

    def test_user_can_still_see_their_own_token(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)

        response = self.client.get("/api/users/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("token", response.data)


class ImpersonationTokenLeakTest(test.APITestCase):
    """`/api/users/me` under impersonation returned the impersonated user's token.

    `_can_see_token` compares against `request.user`, which impersonation has
    already replaced — so "her own token" read as true for the impersonator,
    handing them a durable credential for somebody else.
    """

    def test_impersonator_does_not_receive_the_impersonated_users_token(self):
        staff = structure_factories.UserFactory(is_staff=True)
        victim = structure_factories.UserFactory()
        Token.objects.get_or_create(user=staff)
        Token.objects.get_or_create(user=victim)

        token = Token.objects.get(user=staff)
        response = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
            HTTP_X_IMPERSONATED_USER_UUID=victim.uuid.hex,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], victim.username)
        self.assertNotIn("token", response.data)


@enforce()
class ImpersonationRequiresVerifiedSessionTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.victim = structure_factories.UserFactory()
        PasskeyCredentialFactory(user=self.staff)
        Token.objects.get_or_create(user=self.staff)
        Token.objects.get_or_create(user=self.victim)
        self.token = Token.objects.get(user=self.staff)

    def _impersonate(self):
        return self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Token {self.token.key}",
            HTTP_X_IMPERSONATED_USER_UUID=self.victim.uuid.hex,
        )

    def test_password_only_session_cannot_impersonate(self):
        """Owning a passkey is not the same as having used it."""
        self.assertEqual(self._impersonate().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_passkey_verified_session_can_impersonate(self):
        mark_session_verified(self.token)

        response = self._impersonate()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], self.victim.username)

    def test_rotating_the_token_drops_the_verification(self):
        """refresh_token() deletes and recreates; the proof must not survive."""
        mark_session_verified(self.token)
        self.assertTrue(is_session_verified(self.token))

        self.token.delete()
        new_token = Token.objects.create(user=self.staff)

        self.assertFalse(is_session_verified(new_token))
        self.assertEqual(PasskeyVerifiedSession.objects.count(), 0)


class AdminSiteAccessTest(test.APITestCase):
    """The Django admin login form is not guarded by any passkey ceremony."""

    def test_staff_reach_the_admin_when_enforcement_is_off(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.assertTrue(can_access_admin_site(staff))

    @enforce()
    def test_staff_are_refused_when_enforcement_is_on(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.assertFalse(can_access_admin_site(staff))

    @enforce()
    def test_support_are_refused_too(self):
        """can_access_admin_site grants support the same reach as staff."""
        support = structure_factories.UserFactory(is_support=True)
        self.assertFalse(can_access_admin_site(support))

    @enforce()
    def test_ordinary_users_are_unaffected(self):
        self.assertFalse(can_access_admin_site(structure_factories.UserFactory()))

    @enforce()
    def test_the_login_form_says_why_it_refused(self):
        """Otherwise the page reloads with no message and reads as a broken
        password, when the point is that the admin is not the way in here."""
        from django.forms import ValidationError

        from waldur_core.core.admin import CustomAdminAuthenticationForm

        staff = structure_factories.UserFactory(is_staff=True)
        form = CustomAdminAuthenticationForm()

        with self.assertRaises(ValidationError) as caught:
            form.confirm_login_allowed(staff)

        self.assertEqual(caught.exception.code, "passkey_required")

    def test_the_login_form_is_silent_when_enforcement_is_off(self):
        from waldur_core.core.admin import CustomAdminAuthenticationForm

        staff = structure_factories.UserFactory(is_staff=True)
        CustomAdminAuthenticationForm().confirm_login_allowed(staff)


@enforce()
class PersonalAccessTokenGateTest(test.APITestCase):
    """A PAT is a long-lived, passkey-free credential."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        Token.objects.get_or_create(user=self.staff)
        self.token = Token.objects.get(user=self.staff)

    def _create_pat(self):
        return self.client.post(
            "/api/personal-access-tokens/",
            {
                "name": "ci",
                "scopes": ["STAFF.ACCESS"],
                "expires_at": "2030-01-01T00:00:00Z",
            },
            HTTP_AUTHORIZATION=f"Token {self.token.key}",
            format="json",
        )

    def test_password_only_session_cannot_mint_one(self):
        self.assertEqual(self._create_pat().status_code, status.HTTP_403_FORBIDDEN)

    def test_passkey_verified_session_can(self):
        mark_session_verified(self.token)
        self.assertNotEqual(self._create_pat().status_code, status.HTTP_403_FORBIDDEN)

    def test_listing_stays_open(self):
        """Revocation must not need the factor, or a lost key strands the user."""
        response = self.client.get(
            "/api/personal-access-tokens/",
            HTTP_AUTHORIZATION=f"Token {self.token.key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class EnforcementIsOptInTest(test.APITestCase):
    """None of this may bite a deployment that did not ask for it."""

    def test_nothing_is_enforced_by_default(self):
        from waldur_core.passkeys import policy

        staff = structure_factories.UserFactory(is_staff=True)
        self.assertFalse(policy.is_enforced_for(staff))

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["LOCAL_SIGNIN"],
        PASSKEY_ENFORCED_FOR_STAFF=True,
    )
    def test_enforcement_without_passkeys_enabled_is_inert(self):
        """Otherwise the setting locks staff out with no way to comply."""
        from waldur_core.passkeys import policy

        staff = structure_factories.UserFactory(is_staff=True)
        self.assertFalse(policy.is_enforced_for(staff))


class RevokeUnverifiedStaffTokensTest(test.APITestCase):
    """Enforcement is retrospective or it is nothing.

    Every privileged token that predates the switch was issued without a
    passkey, so leaving them in place means the setting changes nothing until
    each happens to expire.
    """

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.ordinary = structure_factories.UserFactory()
        for user in (self.staff, self.support, self.ordinary):
            Token.objects.get_or_create(user=user)

    def _run(self, **kwargs):
        from django.core.management import call_command

        call_command("revoke_unverified_staff_tokens", **kwargs)

    def test_unverified_privileged_tokens_are_deleted(self):
        self._run()

        self.assertFalse(Token.objects.filter(user=self.staff).exists())
        self.assertFalse(Token.objects.filter(user=self.support).exists())

    def test_ordinary_users_are_left_alone(self):
        self._run()

        self.assertTrue(Token.objects.filter(user=self.ordinary).exists())

    def test_a_passkey_verified_session_survives(self):
        """Someone who already signed in with a passkey is not logged out."""
        mark_session_verified(Token.objects.get(user=self.staff))

        self._run()

        self.assertTrue(Token.objects.filter(user=self.staff).exists())
        self.assertFalse(Token.objects.filter(user=self.support).exists())

    def test_dry_run_changes_nothing(self):
        self._run(dry_run=True)

        self.assertTrue(Token.objects.filter(user=self.staff).exists())

    def test_deactivated_staff_are_included(self):
        """A forgotten credential on a disabled account is the worst case."""
        self.staff.is_active = False
        self.staff.save()

        self._run()

        self.assertFalse(Token.objects.filter(user=self.staff).exists())
