"""One test per bypass named in the issue.

Each of these was a way to obtain a privileged session without satisfying a
passkey. Together they are the difference between enforcement being a property
of the credential and being a property of the login page.
"""

import json

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
from waldur_core.passkeys.tests.authenticator import SoftwareAuthenticator
from waldur_core.passkeys.tests.factories import PasskeyCredentialFactory
from waldur_core.passkeys.tests.helpers import ORIGIN, RP_ID
from waldur_core.structure.tests import factories as structure_factories

User = get_user_model()


def _enrol_admin(user):
    """Give a user a real, assertable credential."""
    from waldur_core.passkeys import services
    from waldur_core.passkeys.tests.authenticator import SoftwareAuthenticator

    authenticator = SoftwareAuthenticator()
    ceremony, options = services.start_registration(user)
    credential = services.finish_registration(
        ceremony,
        authenticator.register(options["challenge"], RP_ID, ORIGIN),
        "Admin key",
    )
    return authenticator, credential


def enforce(**kwargs):
    settings = dict(
        AUTHENTICATION_METHODS=["LOCAL_SIGNIN", "PASSKEY_SIGNIN", "PASSKEY_MFA"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_RP_NAME="Waldur",
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
    """The admin gets a passkey step of its own rather than being closed.

    ``can_access_admin_site`` answers account eligibility only; whether the
    *session* satisfied a passkey is asked by ``CustomAdminSite``, which can
    see the request.
    """

    def test_account_eligibility_is_unchanged_by_enforcement(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.assertTrue(can_access_admin_site(staff))

    @enforce()
    def test_account_eligibility_still_ignores_the_session(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.assertTrue(can_access_admin_site(staff))

    @enforce()
    def test_ordinary_users_are_unaffected(self):
        self.assertFalse(can_access_admin_site(structure_factories.UserFactory()))

    @enforce()
    def test_an_account_with_no_passkey_is_told_to_enrol_in_the_portal(self):
        """The admin has no enrolment flow, so there is nothing to prompt for."""
        from django.forms import ValidationError

        from waldur_core.core.admin import CustomAdminAuthenticationForm

        staff = structure_factories.UserFactory(is_staff=True)

        with self.assertRaises(ValidationError) as caught:
            CustomAdminAuthenticationForm().confirm_login_allowed(staff)

        self.assertEqual(caught.exception.code, "passkey_required")

    @enforce()
    def test_an_account_with_a_passkey_proceeds_to_the_challenge(self):
        from waldur_core.core.admin import CustomAdminAuthenticationForm

        staff = structure_factories.UserFactory(is_staff=True)
        PasskeyCredentialFactory(user=staff)

        # No exception: the password half succeeded, the passkey step follows.
        CustomAdminAuthenticationForm().confirm_login_allowed(staff)

    def test_the_login_form_is_silent_when_enforcement_is_off(self):
        from waldur_core.core.admin import CustomAdminAuthenticationForm

        staff = structure_factories.UserFactory(is_staff=True)
        CustomAdminAuthenticationForm().confirm_login_allowed(staff)


@enforce()
class AdminPasskeyFlowTest(test.APITestCase):
    """Password alone must not reach the admin; a passkey completes it."""

    def setUp(self):
        self.password = "very-secret-password"
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.staff.set_password(self.password)
        self.staff.save()
        self.authenticator, self.credential = _enrol_admin(self.staff)

    def test_password_login_lands_on_the_passkey_step_not_the_index(self):
        self.client.force_login(self.staff)

        response = self.client.get("/admin/")

        # admin_view refuses, redirects to the admin login, which forwards to
        # the challenge rather than showing the form again.
        self.assertEqual(response.status_code, 302)
        follow = self.client.get("/admin/login/")
        self.assertEqual(follow.status_code, 302)
        self.assertIn("/admin/passkey/", follow["Location"])

    def test_the_challenge_page_renders_for_a_user_with_a_credential(self):
        self.client.force_login(self.staff)

        response = self.client.get("/admin/passkey/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["has_credential"])

    def test_the_challenge_page_says_so_when_there_is_no_credential(self):
        bare = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(bare)

        response = self.client.get("/admin/passkey/")

        self.assertFalse(response.context["has_credential"])

    def test_a_verified_assertion_opens_the_admin(self):
        self.client.force_login(self.staff)

        options = self.client.post("/admin/passkey/options/").json()["options"]
        assertion = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)
        verified = self.client.post(
            "/admin/passkey/verify/",
            data=json.dumps({"credential": assertion}),
            content_type="application/json",
        )

        self.assertEqual(verified.status_code, 200)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_bad_assertion_does_not_open_the_admin(self):
        self.client.force_login(self.staff)

        options = self.client.post("/admin/passkey/options/").json()["options"]
        assertion = self.authenticator.authenticate(
            options["challenge"], RP_ID, ORIGIN, corrupt_signature=True
        )
        verified = self.client.post(
            "/admin/passkey/verify/",
            data=json.dumps({"credential": assertion}),
            content_type="application/json",
        )

        self.assertEqual(verified.status_code, 400)
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_verify_without_a_ceremony_is_refused(self):
        """The handle lives in the session, so it cannot be supplied by hand."""
        self.client.force_login(self.staff)

        response = self.client.post(
            "/admin/passkey/verify/",
            data=json.dumps({"credential": {}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_another_users_passkey_does_not_satisfy_the_challenge(self):
        other = structure_factories.UserFactory(is_staff=True)
        other_authenticator, _ = _enrol_admin(other)
        self.client.force_login(self.staff)

        options = self.client.post("/admin/passkey/options/").json()["options"]
        assertion = other_authenticator.authenticate(
            options["challenge"], RP_ID, ORIGIN
        )
        verified = self.client.post(
            "/admin/passkey/verify/",
            data=json.dumps({"credential": assertion}),
            content_type="application/json",
        )

        self.assertEqual(verified.status_code, 400)

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        response = self.client.get("/admin/passkey/")
        self.assertEqual(response.status_code, 302)


@enforce()
class PromotedStaffCanStillEnrolTest(test.APITestCase):
    """An existing user marked as staff after enforcement is switched on.

    They hold no passkey, and enforcement demands one — so the question is
    whether they can still reach the one thing that would let them comply.
    Nothing in the closure list may block enrolment itself, or promoting a
    user becomes a way to lock them out permanently.
    """

    def setUp(self):
        self.password = "very-secret-password"
        self.user = structure_factories.UserFactory()
        self.user.set_password(self.password)
        self.user.save()
        # Promoted after the fact, which is the realistic case.
        self.user.is_staff = True
        self.user.save()

    def test_password_login_still_issues_a_token(self):
        """A user with no credential is not held at a second factor.

        Otherwise there would be no way to obtain the session that enrolment
        requires.
        """
        response = self.client.post(
            "/api-auth/password/",
            {"username": self.user.username, "password": self.password},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("token", response.data)

    def test_they_can_enrol_with_that_token(self):
        token = self.client.post(
            "/api-auth/password/",
            {"username": self.user.username, "password": self.password},
        ).data["token"]

        begin = self.client.post(
            "/api/passkeys/registration/begin/",
            HTTP_AUTHORIZATION=f"Token {token}",
        )
        self.assertEqual(begin.status_code, status.HTTP_200_OK)

        authenticator = SoftwareAuthenticator()
        finished = self.client.post(
            "/api/passkeys/registration/finish/",
            {
                "ceremony": begin.data["ceremony"],
                "name": "First key",
                "credential": authenticator.register(
                    begin.data["options"]["challenge"], RP_ID, ORIGIN
                ),
            },
            HTTP_AUTHORIZATION=f"Token {token}",
            format="json",
        )

        self.assertEqual(finished.status_code, status.HTTP_201_CREATED)

    def test_enrolment_then_unlocks_the_privileged_paths(self):
        """The loop closes: enrol, sign in with the passkey, impersonate."""
        token = self.client.post(
            "/api-auth/password/",
            {"username": self.user.username, "password": self.password},
        ).data["token"]
        authenticator, _ = _enrol_admin(self.user)

        victim = structure_factories.UserFactory()
        Token.objects.get_or_create(user=victim)

        # Password-only session still cannot impersonate.
        blocked = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Token {token}",
            HTTP_X_IMPERSONATED_USER_UUID=victim.uuid.hex,
        )
        self.assertEqual(blocked.status_code, status.HTTP_401_UNAUTHORIZED)

        # Signing in with the passkey yields a verified session that can.
        begin = self.client.post("/api/passkeys/signin/begin/", {})
        assertion = authenticator.authenticate(
            begin.data["options"]["challenge"], RP_ID, ORIGIN
        )
        verified_token = self.client.post(
            "/api/passkeys/signin/finish/",
            {"ceremony": begin.data["ceremony"], "credential": assertion},
            format="json",
        ).data["token"]

        allowed = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Token {verified_token}",
            HTTP_X_IMPERSONATED_USER_UUID=victim.uuid.hex,
        )
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)
        self.assertEqual(allowed.data["username"], victim.username)

    def test_the_admin_tells_them_where_to_enrol(self):
        """The admin has no enrolment flow, so it must not just refuse."""
        from django.forms import ValidationError

        from waldur_core.core.admin import CustomAdminAuthenticationForm

        with self.assertRaises(ValidationError) as caught:
            CustomAdminAuthenticationForm().confirm_login_allowed(self.user)

        self.assertEqual(caught.exception.code, "passkey_required")
        self.assertIn("portal", str(caught.exception.messages[0]))


class AdminUnaffectedWithoutEnforcementTest(test.APITestCase):
    def test_staff_reach_the_admin_directly(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(staff)

        self.assertEqual(self.client.get("/admin/").status_code, 200)


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
