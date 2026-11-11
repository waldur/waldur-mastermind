"""Step-up re-authentication before sensitive account changes.

Every user has one API token and every sign-in returns the same key, so
nothing attached to the user or the token tells the person who just signed in
from somebody holding a copy. The guarded actions therefore ask for a grant: a
new secret, returned only to whoever has just proved again who they are.
"""

import importlib
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.throttling import ScopedRateThrottle

from waldur_auth_social.models import IdentityProvider
from waldur_core.configuration.testing import override_config
from waldur_core.core import reauthentication
from waldur_core.core.authentication import refresh_token
from waldur_core.core.models import (
    ChangeEmailRequest,
    PersonalAccessToken,
    ReauthenticationGrant,
    User,
)
from waldur_core.core.tests.helpers import create_pat, override_waldur_core_settings
from waldur_core.logging.models import Event, Feed
from waldur_core.passkeys import services as passkey_services
from waldur_core.passkeys.models import PasskeyCeremony, PasskeyCredential
from waldur_core.passkeys.tests.authenticator import SoftwareAuthenticator
from waldur_core.passkeys.tests.helpers import ORIGIN, RP_ID, enable_passkeys
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.tests import factories as structure_factories

PASSWORD = "Correct-Horse-Battery-9"
PAT_URL = "/api/personal-access-tokens/"
PASSKEY_REGISTRATION_BEGIN_URL = "/api/passkeys/registration/begin/"
PASSWORD_URL = "/api/reauthentication/password/"
PASSKEY_BEGIN_URL = "/api/reauthentication/passkey/begin/"
PASSKEY_FINISH_URL = "/api/reauthentication/passkey/finish/"
LOGIN_URL = "/api-auth/password/"

TOKEN_LIFETIME = "token_lifetime"
CHANGE_EMAIL = "change_email"
REGISTER_PASSKEY = "register_passkey"
CREATE_PAT = "create_personal_access_token"
REVOKE_PASSKEY = "revoke_passkey"
CHANGE_PASSWORD = "change_password"


def make_user(**kwargs):
    kwargs.setdefault("agreement_date", timezone.now())
    user = structure_factories.UserFactory(**kwargs)
    user.set_password(PASSWORD)
    user.save()
    return user


def enrol_passkey(user):
    authenticator = SoftwareAuthenticator()
    ceremony, options = passkey_services.start_registration(user)
    passkey_services.finish_registration(
        ceremony,
        authenticator.register(options["challenge"], RP_ID, ORIGIN),
        "Laptop",
    )
    return authenticator


def grant_header(secret):
    return {"HTTP_X_REAUTHENTICATION_GRANT": secret}


class TokenHolderTest(test.APITestCase):
    """Whoever holds the API token, and nothing else."""

    def setUp(self):
        # The login lockout counters live in the cache.
        cache.clear()
        self.user = make_user(token_lifetime=3600, can_use_personal_access_tokens=True)
        self.authenticate(self.user)
        self.user_url = structure_factories.UserFactory.get_url(self.user)

    def tearDown(self):
        cache.clear()

    def authenticate(self, user, **credentials):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token.key, **credentials)

    def change_token_lifetime(self, **headers):
        return self.client.patch(
            self.user_url, {"token_lifetime": None}, format="json", **headers
        )

    def change_email(self, **headers):
        return self.client.post(
            self.user_url + "change_email/",
            {"email": "elsewhere@example.com"},
            format="json",
            **headers,
        )

    def begin_passkey_registration(self, **headers):
        return self.client.post(PASSKEY_REGISTRATION_BEGIN_URL, {}, **headers)

    def create_pat(self, **headers):
        return self.client.post(
            PAT_URL,
            {
                "name": "Kept",
                "scopes": [PermissionEnum.LIST_ORDERS.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
            **headers,
        )

    def reauthenticate(self, action, password=PASSWORD, **extra):
        return self.client.post(
            PASSWORD_URL, {"action": action, "password": password}, **extra
        )

    def grant_for(self, action):
        response = self.reauthenticate(action)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data["grant"]

    def assert_challenge(self, response, action, methods=("password",)):
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            response["WWW-Authenticate"],
            'Token error="insufficient_user_authentication"',
        )
        self.assertEqual(response.data["code"], "reauthentication_required")
        self.assertEqual(response.data["action"], action)
        self.assertEqual(response.data["methods"], list(methods))


class TokenAloneIsNotEnoughTest(TokenHolderTest):
    """Each guarded action, sent with only the API token, changes nothing."""

    def test_token_lifetime(self):
        response = self.change_token_lifetime()

        self.assert_challenge(response, TOKEN_LIFETIME)
        self.user.refresh_from_db()
        self.assertEqual(self.user.token_lifetime, 3600)

    def test_change_email(self):
        response = self.change_email()

        self.assert_challenge(response, CHANGE_EMAIL)
        self.assertFalse(ChangeEmailRequest.objects.filter(user=self.user).exists())

    @enable_passkeys()
    def test_passkey_registration(self):
        response = self.begin_passkey_registration()

        self.assert_challenge(response, REGISTER_PASSKEY)
        self.assertFalse(PasskeyCeremony.objects.filter(user=self.user).exists())

    def test_personal_access_token(self):
        response = self.create_pat()

        self.assert_challenge(response, CREATE_PAT)
        self.assertFalse(PersonalAccessToken.objects.filter(user=self.user).exists())

    def test_personal_access_token_rotation(self):
        pat, _ = create_pat(self.user)

        response = self.client.post(f"{PAT_URL}{pat.uuid.hex}/rotate/")

        self.assert_challenge(response, CREATE_PAT)
        pat.refresh_from_db()
        self.assertTrue(pat.is_active)
        self.assertEqual(PersonalAccessToken.objects.filter(user=self.user).count(), 1)

    def test_staff_writing_an_email(self):
        staff = make_user(is_staff=True)
        self.authenticate(staff)

        response = self.client.patch(self.user_url, {"email": "new@example.com"})

        self.assert_challenge(response, CHANGE_EMAIL)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.email, "new@example.com")


class PasswordGrantTest(TokenHolderTest):
    """With a grant from re-entering the password, each action succeeds."""

    def test_token_lifetime(self):
        response = self.change_token_lifetime(
            **grant_header(self.grant_for(TOKEN_LIFETIME))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.token_lifetime)

    def test_change_email(self):
        response = self.change_email(**grant_header(self.grant_for(CHANGE_EMAIL)))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(ChangeEmailRequest.objects.filter(user=self.user).exists())

    @enable_passkeys()
    def test_passkey_registration(self):
        response = self.begin_passkey_registration(
            **grant_header(self.grant_for(REGISTER_PASSKEY))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("challenge", response.data["options"])

    def test_personal_access_token(self):
        response = self.create_pat(**grant_header(self.grant_for(CREATE_PAT)))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(PersonalAccessToken.objects.filter(user=self.user).exists())

    def test_personal_access_token_rotation(self):
        pat, _ = create_pat(self.user)

        response = self.client.post(
            f"{PAT_URL}{pat.uuid.hex}/rotate/",
            **grant_header(self.grant_for(CREATE_PAT)),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        pat.refresh_from_db()
        self.assertFalse(pat.is_active)

    def test_staff_on_another_users_account_uses_their_own_grant(self):
        staff = make_user(is_staff=True)
        self.authenticate(staff)

        response = self.client.patch(
            self.user_url,
            {"email": "new@example.com"},
            **grant_header(self.grant_for(CHANGE_EMAIL)),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.com")
        self.assertTrue(
            ReauthenticationGrant.objects.filter(
                user=staff, used_at__isnull=False
            ).exists()
        )

    def test_the_users_grant_does_not_serve_staff(self):
        secret = self.grant_for(CHANGE_EMAIL)
        staff = make_user(is_staff=True)
        self.authenticate(staff)

        response = self.client.patch(
            self.user_url, {"email": "new@example.com"}, **grant_header(secret)
        )

        self.assert_challenge(response, CHANGE_EMAIL)

    def test_grant_response(self):
        now = timezone.now()
        with freeze_time(now):
            response = self.reauthenticate(CHANGE_EMAIL)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["action"], CHANGE_EMAIL)
        self.assertEqual(response.data["expires_at"], now + timedelta(seconds=300))
        grant = ReauthenticationGrant.objects.get(user=self.user)
        self.assertEqual(grant.method, "password")
        self.assertNotEqual(grant.secret_hash, response.data["grant"])
        self.assertEqual(
            grant.secret_hash,
            ReauthenticationGrant.hash_secret(response.data["grant"]),
        )

    @override_config(REAUTHENTICATION_GRANT_LIFETIME_SECONDS=60)
    def test_lifetime_comes_from_the_setting(self):
        now = timezone.now()
        with freeze_time(now):
            response = self.reauthenticate(CHANGE_EMAIL)

        self.assertEqual(response.data["expires_at"], now + timedelta(seconds=60))

    def test_unchanged_values_need_no_grant(self):
        # Edit forms send every field back.
        response = self.client.patch(
            self.user_url,
            {
                "token_lifetime": 3600,
                "email": self.user.email,
                "job_title": "Engineer",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.job_title, "Engineer")

    def test_email_and_lifetime_are_changed_separately(self):
        staff = make_user(is_staff=True)
        self.authenticate(staff)

        response = self.client.patch(
            structure_factories.UserFactory.get_url(staff),
            {"email": "new@example.com", "token_lifetime": None},
            format="json",
            **grant_header(self.grant_for(CHANGE_EMAIL)),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        staff.refresh_from_db()
        self.assertNotEqual(staff.email, "new@example.com")

    def test_invalid_change_is_refused_before_the_grant_is_asked_for(self):
        response = self.client.post(
            self.user_url + "change_email/", {"email": "not-an-email"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GrantRulesTest(TokenHolderTest):
    """A grant is single-use, for one action and one user, and expires."""

    def test_it_cannot_be_used_twice(self):
        secret = self.grant_for(CREATE_PAT)
        self.assertEqual(
            self.create_pat(**grant_header(secret)).status_code,
            status.HTTP_201_CREATED,
        )

        response = self.create_pat(**grant_header(secret))

        self.assert_challenge(response, CREATE_PAT)
        self.assertEqual(PersonalAccessToken.objects.filter(user=self.user).count(), 1)

    def test_it_cannot_be_used_for_another_action(self):
        secret = self.grant_for(CHANGE_EMAIL)

        response = self.change_token_lifetime(**grant_header(secret))

        self.assert_challenge(response, TOKEN_LIFETIME)
        # Still good for what it was issued for.
        self.assertEqual(
            self.change_email(**grant_header(secret)).status_code, status.HTTP_200_OK
        )

    def test_it_cannot_be_used_by_another_user(self):
        secret = self.grant_for(CREATE_PAT)
        other = make_user(can_use_personal_access_tokens=True)
        self.authenticate(other)

        response = self.create_pat(**grant_header(secret))

        self.assert_challenge(response, CREATE_PAT)
        self.assertFalse(PersonalAccessToken.objects.exists())

    def test_it_cannot_be_used_after_it_expires(self):
        with freeze_time(timezone.now() - timedelta(seconds=301)):
            secret = self.grant_for(CREATE_PAT)

        response = self.create_pat(**grant_header(secret))

        self.assert_challenge(response, CREATE_PAT)

    def test_an_unknown_grant_is_refused(self):
        response = self.create_pat(**grant_header("made-up"))

        self.assert_challenge(response, CREATE_PAT)

    def test_a_failed_action_leaves_the_grant_unused(self):
        pat, _ = create_pat(self.user, is_active=False)
        secret = self.grant_for(CREATE_PAT)

        response = self.client.post(
            f"{PAT_URL}{pat.uuid.hex}/rotate/", **grant_header(secret)
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(ReauthenticationGrant.objects.get(user=self.user).used_at)
        self.assertEqual(
            self.create_pat(**grant_header(secret)).status_code,
            status.HTTP_201_CREATED,
        )


@enable_passkeys()
class PasskeyGrantTest(TokenHolderTest):
    def setUp(self):
        super().setUp()
        self.authenticator = enrol_passkey(self.user)

    def begin(self, action=CREATE_PAT):
        response = self.client.post(PASSKEY_BEGIN_URL, {"action": action})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def finish(self, begin, action=CREATE_PAT, authenticator=None, **kwargs):
        authenticator = authenticator or self.authenticator
        return self.client.post(
            PASSKEY_FINISH_URL,
            {
                "action": action,
                "ceremony": str(begin["ceremony"]),
                "credential": authenticator.authenticate(
                    begin["options"]["challenge"], RP_ID, ORIGIN, **kwargs
                ),
            },
            format="json",
        )

    def test_begin_asks_for_user_verification_with_the_users_own_passkeys(self):
        options = self.begin()["options"]

        self.assertEqual(options["userVerification"], "required")
        self.assertEqual(len(options["allowCredentials"]), 1)

    def test_begin_records_the_action(self):
        begin = self.begin(CHANGE_EMAIL)

        ceremony = PasskeyCeremony.objects.get(uuid=begin["ceremony"])
        self.assertEqual(ceremony.reauthentication_action, CHANGE_EMAIL)

    def test_finish_for_another_action_is_refused(self):
        begin = self.begin(CREATE_PAT)

        response = self.finish(begin, action=CHANGE_EMAIL)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ReauthenticationGrant.objects.exists())
        # The ceremony is still good for the change it was begun for.
        response = self.finish(begin, action=CREATE_PAT)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["action"], CREATE_PAT)

    def test_assertion_issues_a_grant_that_works(self):
        response = self.finish(self.begin())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["action"], CREATE_PAT)
        self.assertEqual(
            ReauthenticationGrant.objects.get(user=self.user).method, "passkey"
        )
        self.assertEqual(
            self.create_pat(**grant_header(response.data["grant"])).status_code,
            status.HTTP_201_CREATED,
        )

    def test_a_bad_assertion_issues_nothing(self):
        response = self.finish(self.begin(), corrupt_signature=True)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ReauthenticationGrant.objects.exists())
        self.assertTrue(
            Event.objects.filter(event_type="reauthentication_failed").exists()
        )

    def test_an_assertion_without_user_verification_issues_nothing(self):
        self.authenticator.user_verified = False

        response = self.finish(self.begin())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ReauthenticationGrant.objects.exists())

    def test_another_users_ceremony_is_refused(self):
        begin = self.begin()
        other = make_user()
        other_authenticator = enrol_passkey(other)
        self.authenticate(other)

        response = self.finish(begin, authenticator=other_authenticator)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ReauthenticationGrant.objects.exists())

    def test_a_sign_in_ceremony_is_not_accepted(self):
        ceremony, options = passkey_services.start_signin()

        response = self.finish({"ceremony": ceremony.uuid, "options": options})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ReauthenticationGrant.objects.exists())

    def test_a_user_without_a_passkey_cannot_begin(self):
        self.authenticate(make_user())

        response = self.client.post(PASSKEY_BEGIN_URL, {"action": CREATE_PAT})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_passkey_user_can_register_another_passkey(self):
        begin = self.begin(REGISTER_PASSKEY)
        secret = self.finish(begin, REGISTER_PASSKEY).data["grant"]

        response = self.begin_passkey_registration(**grant_header(secret))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


def enforce_passkeys_for_staff():
    return override_waldur_core_settings(
        AUTHENTICATION_METHODS=["LOCAL_SIGNIN", "PASSKEY_SIGNIN"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_RP_NAME="Waldur",
        PASSKEY_ALLOWED_ORIGINS=[ORIGIN],
        PASSKEY_ENFORCED_FOR_STAFF=True,
    )


class AvailableMethodsTest(TokenHolderTest):
    """The challenge names the methods this user can re-authenticate with."""

    @enable_passkeys(mfa=False)
    def test_password_and_passkey(self):
        # Without the second factor, a password alone signs this user in.
        enrol_passkey(self.user)

        self.assert_challenge(
            self.create_pat(), CREATE_PAT, methods=["password", "passkey"]
        )

    @enable_passkeys()
    def test_only_the_passkey_where_password_sign_in_also_needs_it(self):
        enrol_passkey(self.user)

        self.assert_challenge(self.create_pat(), CREATE_PAT, methods=["passkey"])
        self.assertEqual(
            self.reauthenticate(CREATE_PAT).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertFalse(ReauthenticationGrant.objects.exists())

    @enforce_passkeys_for_staff()
    def test_only_the_passkey_for_staff_under_enforcement_who_have_one(self):
        self.user.is_staff = True
        self.user.save()
        enrol_passkey(self.user)

        self.assert_challenge(self.change_email(), CHANGE_EMAIL, methods=["passkey"])
        self.assertEqual(
            self.reauthenticate(CHANGE_EMAIL).status_code, status.HTTP_400_BAD_REQUEST
        )

    @enforce_passkeys_for_staff()
    def test_the_password_for_staff_under_enforcement_without_a_passkey(self):
        # They have to enrol one, and a password is all they can sign in with.
        self.user.is_staff = True
        self.user.save()

        self.assert_challenge(
            self.begin_passkey_registration(), REGISTER_PASSKEY, methods=["password"]
        )

    @enforce_passkeys_for_staff()
    def test_the_password_for_staff_whose_passkeys_are_for_another_rp_id(self):
        # A passkey that cannot be asserted here does not take the password away.
        self.user.is_staff = True
        self.user.save()
        enrol_passkey(self.user)
        PasskeyCredential.objects.filter(user=self.user).update(rp_id="old.example.org")

        self.assert_challenge(
            self.begin_passkey_registration(), REGISTER_PASSKEY, methods=["password"]
        )

    @enable_passkeys()
    def test_passkey_only(self):
        enrol_passkey(self.user)
        self.user.set_unusable_password()
        self.user.save()

        self.assert_challenge(self.create_pat(), CREATE_PAT, methods=["passkey"])
        self.assertEqual(
            self.reauthenticate(CREATE_PAT).status_code, status.HTTP_400_BAD_REQUEST
        )

    @override_waldur_core_settings(AUTHENTICATION_METHODS=["SOCIAL_SIGNUP"])
    def test_password_is_not_offered_when_local_sign_in_is_disabled(self):
        self.assert_challenge(self.create_pat(), CREATE_PAT, methods=[])
        self.assertEqual(
            self.reauthenticate(CREATE_PAT).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertFalse(ReauthenticationGrant.objects.exists())

    def test_a_user_without_methods_is_refused_and_pointed_to_support(self):
        self.user.set_unusable_password()
        self.user.save()

        response = self.change_token_lifetime()

        self.assert_challenge(response, TOKEN_LIFETIME, methods=[])
        self.assertIn("contact support", response.data["detail"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.token_lifetime, 3600)


@enable_passkeys()
class SecondFactorBypassTest(TokenHolderTest):
    """A token and the password, without the authenticator, must not enrol a
    passkey of the holder's own, which would sign in alone from then on."""

    def setUp(self):
        super().setUp()
        enrol_passkey(self.user)

    def test_password_alone_cannot_get_a_grant_to_register_a_passkey(self):
        registrations = PasskeyCeremony.objects.filter(
            user=self.user, kind="registration"
        )
        enrolled = registrations.count()

        response = self.reauthenticate(REGISTER_PASSKEY)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ReauthenticationGrant.objects.exists())
        registration = self.begin_passkey_registration()
        self.assert_challenge(registration, REGISTER_PASSKEY, methods=["passkey"])
        self.assertEqual(registrations.count(), enrolled)

    def test_password_alone_cannot_get_a_grant_to_revoke_the_passkey(self):
        credential = PasskeyCredential.objects.get(user=self.user)

        self.assertEqual(
            self.reauthenticate(REVOKE_PASSKEY).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        response = self.client.post(f"/api/passkeys/{credential.uuid.hex}/revoke/")

        self.assert_challenge(response, REVOKE_PASSKEY, methods=["passkey"])
        credential.refresh_from_db()
        self.assertTrue(credential.is_active)


@enable_passkeys(mfa=False)
class PasskeyRevocationTest(TokenHolderTest):
    def setUp(self):
        super().setUp()
        enrol_passkey(self.user)
        self.credential = PasskeyCredential.objects.get(user=self.user)

    def revoke(self, url=None, **extra):
        url = url or f"/api/passkeys/{self.credential.uuid.hex}/revoke/"
        return self.client.post(url, {"reason": "Lost it"}, **extra)

    def test_token_alone_cannot_revoke_a_passkey(self):
        self.assert_challenge(
            self.revoke(), REVOKE_PASSKEY, methods=["password", "passkey"]
        )
        self.credential.refresh_from_db()
        self.assertTrue(self.credential.is_active)

    def test_a_grant_revokes_it(self):
        response = self.revoke(**grant_header(self.grant_for(REVOKE_PASSKEY)))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.credential.refresh_from_db()
        self.assertFalse(self.credential.is_active)

    def test_staff_revoke_needs_the_staff_members_own_grant(self):
        staff = make_user(is_staff=True)
        url = f"/api/staff-passkeys/{self.credential.uuid.hex}/revoke/"
        users_grant = self.grant_for(REVOKE_PASSKEY)
        self.authenticate(staff)

        self.assert_challenge(
            self.revoke(url, **grant_header(users_grant)), REVOKE_PASSKEY
        )
        response = self.revoke(url, **grant_header(self.grant_for(REVOKE_PASSKEY)))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.credential.refresh_from_db()
        self.assertFalse(self.credential.is_active)


class PasswordChangeTest(TokenHolderTest):
    """Staff set passwords without the current one, so a staff API token on
    its own account would become a password, and the password any grant."""

    def setUp(self):
        super().setUp()
        self.staff = make_user(is_staff=True)
        self.authenticate(self.staff)

    def set_password(self, user, **extra):
        return self.client.post(
            structure_factories.UserFactory.get_url(user, "change_password"),
            {"new_password": "Another-Horse-Battery-7"},
            **extra,
        )

    def assert_password_kept(self, user):
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_staff_token_alone_cannot_set_its_own_password(self):
        response = self.set_password(self.staff)

        self.assert_challenge(response, CHANGE_PASSWORD)
        self.assert_password_kept(self.staff)
        # So it cannot turn the token into a grant for anything else.
        self.assertEqual(
            self.reauthenticate(
                CREATE_PAT, password="Another-Horse-Battery-7"
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertFalse(ReauthenticationGrant.objects.exists())

    def test_staff_token_alone_cannot_set_another_users_password(self):
        self.assert_challenge(self.set_password(self.user), CHANGE_PASSWORD)
        self.assert_password_kept(self.user)

    def test_with_the_staff_members_own_grant(self):
        # Their own account last: re-authenticating needs the old password.
        for user in (self.user, self.staff):
            with self.subTest(user=user.username):
                response = self.set_password(
                    user, **grant_header(self.grant_for(CHANGE_PASSWORD))
                )

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                user.refresh_from_db()
                self.assertTrue(user.check_password("Another-Horse-Battery-7"))

    def remove_password(self, user, **extra):
        return self.client.post(
            structure_factories.UserFactory.get_url(user, "remove_password"), **extra
        )

    def test_staff_token_alone_cannot_remove_its_own_password(self):
        # A password removed leaves the account with nothing to re-authenticate
        # with, which must not open the way the waiver leaves for accounts that
        # never had one.
        self.assert_challenge(self.remove_password(self.staff), CHANGE_PASSWORD)
        self.assert_password_kept(self.staff)

    def test_removing_a_password_with_the_staff_members_own_grant(self):
        response = self.remove_password(
            self.user, **grant_header(self.grant_for(CHANGE_PASSWORD))
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_usable_password())

    def test_another_users_grant_does_not_serve(self):
        self.authenticate(self.user)
        users_grant = self.grant_for(CHANGE_PASSWORD)
        self.authenticate(self.staff)

        self.assert_challenge(
            self.set_password(self.user, **grant_header(users_grant)), CHANGE_PASSWORD
        )
        self.assert_password_kept(self.user)

    def test_refused_while_impersonating(self):
        other_staff = make_user(is_staff=True)
        refresh_token(other_staff)
        self.authenticate(
            self.staff, HTTP_X_IMPERSONATED_USER_UUID=other_staff.uuid.hex
        )

        response = self.set_password(other_staff)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assert_password_kept(other_staff)

    def test_events(self):
        self.set_password(self.user, **grant_header(self.grant_for(CHANGE_PASSWORD)))

        used = Event.objects.get(event_type="reauthentication_grant_used")
        self.assertEqual(used.context["reauthentication_action"], CHANGE_PASSWORD)
        self.assertEqual(used.context["affected_user_username"], self.staff.username)


class DelegatedCredentialTest(TokenHolderTest):
    """A personal access token or an OIDC access token acts for the user in a
    script or another application, and never on these settings."""

    def use_personal_access_token(self):
        _pat, token = create_pat(self.user, scopes=[PermissionEnum.LIST_ORDERS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def use_oidc_access_token(self):
        self.client.credentials()
        self.client.force_authenticate(self.user, token={"sub": "someone"})

    def assert_refused(self, response):
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("personal access token", str(response.data["detail"]))

    @override_config(PAT_ENABLED=True)
    def test_personal_access_token_gets_no_grant(self):
        self.use_personal_access_token()

        self.assert_refused(self.reauthenticate(CHANGE_EMAIL))
        self.assertFalse(ReauthenticationGrant.objects.exists())

    @override_config(PAT_ENABLED=True)
    def test_personal_access_token_cannot_use_a_grant(self):
        _grant, secret = ReauthenticationGrant.issue(
            self.user, CHANGE_EMAIL, "password", timedelta(minutes=5)
        )
        self.use_personal_access_token()

        self.assert_refused(self.change_email(**grant_header(secret)))
        self.assertFalse(ChangeEmailRequest.objects.exists())
        self.assertIsNone(ReauthenticationGrant.objects.get().used_at)

    @enable_passkeys()
    @override_config(PAT_ENABLED=True)
    def test_personal_access_token_cannot_begin_a_passkey_ceremony(self):
        enrol_passkey(self.user)
        self.use_personal_access_token()

        self.assert_refused(
            self.client.post(PASSKEY_BEGIN_URL, {"action": CHANGE_EMAIL})
        )
        self.assertFalse(
            PasskeyCeremony.objects.filter(kind="reauthentication").exists()
        )

    def test_oidc_access_token_gets_no_grant_and_cannot_use_one(self):
        _grant, secret = ReauthenticationGrant.issue(
            self.user, CHANGE_EMAIL, "password", timedelta(minutes=5)
        )
        self.use_oidc_access_token()

        self.assert_refused(self.reauthenticate(CHANGE_EMAIL))
        self.assert_refused(self.change_email(**grant_header(secret)))
        self.assertEqual(ReauthenticationGrant.objects.count(), 1)
        self.assertFalse(ChangeEmailRequest.objects.exists())

    def test_refusal_is_recorded(self):
        self.use_oidc_access_token()

        self.change_email()

        event = Event.objects.get(event_type="reauthentication_refused")
        self.assertIn("delegated", event.message)


def oidc_provider(**kwargs):
    return IdentityProvider.objects.create(
        provider="keycloak", is_active=True, label="Example SSO", **kwargs
    )


class IdentityProviderEmailTest(TokenHolderTest):
    """An email the identity provider supplies is written back at every
    sign-in, so it is changed at the provider, not here."""

    message = "Your email address comes from Example SSO; change it there."

    def setUp(self):
        super().setUp()
        self.user.registration_method = "keycloak"
        self.user.save()

    def protected_fields(self):
        return self.client.get(self.user_url).data["identity_provider_fields"]

    def test_email_the_provider_maps_cannot_be_changed(self):
        oidc_provider(attribute_mapping={"email": "email"})

        self.assertIn("email", self.protected_fields())
        response = self.change_email(**grant_header(self.grant_for(CHANGE_EMAIL)))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], self.message)
        self.assertFalse(ChangeEmailRequest.objects.exists())

    def test_staff_cannot_write_it_either(self):
        oidc_provider(attribute_mapping={"email": "email"})
        old_email = self.user.email
        staff = make_user(is_staff=True)
        self.authenticate(staff)

        response = self.client.patch(
            self.user_url,
            {"email": "new@example.com"},
            **grant_header(self.grant_for(CHANGE_EMAIL)),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["email"], [self.message])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, old_email)

    def test_email_the_provider_looks_accounts_up_by_cannot_be_changed(self):
        oidc_provider(user_field="email", user_claim="email")

        self.assertIn("email", self.protected_fields())
        self.assertEqual(self.change_email().status_code, status.HTTP_400_BAD_REQUEST)

    def test_email_the_provider_does_not_map_can_be_changed(self):
        oidc_provider(attribute_mapping={"first_name": "given_name"})

        self.assertNotIn("email", self.protected_fields())
        response = self.change_email(**grant_header(self.grant_for(CHANGE_EMAIL)))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_email_set_in_waldur_can_be_changed(self):
        # The provider sent none, so the user or staff set one here.
        oidc_provider(attribute_mapping={"email": "email"})
        for source in (User.SELF_SET_SOURCE, User.STAFF_SET_SOURCE):
            with self.subTest(source=source):
                self.user.record_email_source(source)
                self.user.save()

                self.assertNotIn("email", self.protected_fields())
                response = self.change_email(
                    **grant_header(self.grant_for(CHANGE_EMAIL))
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_record_for_another_address_does_not_apply(self):
        oidc_provider(attribute_mapping={"email": "email"})
        self.user.record_email_source(User.SELF_SET_SOURCE)
        self.user.email = "from-the-provider@example.com"
        self.user.save()

        self.assertIn("email", self.protected_fields())


class NoMethodWaiverTest(TokenHolderTest):
    """Until sign-in through an identity provider can re-authenticate, a user
    with no method at all may still change their own email: the code sent to
    the new address confirms it, and the change is recorded."""

    def setUp(self):
        super().setUp()
        self.user.registration_method = "keycloak"
        self.user.set_unusable_password()
        self.user.save()
        oidc_provider(attribute_mapping={"first_name": "given_name"})

    def waived(self):
        return Event.objects.filter(event_type="reauthentication_waived")

    def test_own_email_change_goes_through_and_is_recorded(self):
        response = self.change_email()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(ChangeEmailRequest.objects.filter(user=self.user).exists())
        event = self.waived().get()
        self.assertEqual(event.context["reauthentication_action"], CHANGE_EMAIL)
        self.assertEqual(event.context["affected_user_username"], self.user.username)

    def test_other_actions_still_get_the_challenge(self):
        self.assert_challenge(self.change_token_lifetime(), TOKEN_LIFETIME, methods=[])
        self.assert_challenge(self.create_pat(), CREATE_PAT, methods=[])
        with enable_passkeys():
            self.assert_challenge(
                self.begin_passkey_registration(), REGISTER_PASSKEY, methods=[]
            )
        self.assertFalse(self.waived().exists())

    def test_a_user_with_a_password_still_needs_a_grant(self):
        self.user.set_password(PASSWORD)
        self.user.save()

        self.assert_challenge(self.change_email(), CHANGE_EMAIL)
        self.assertFalse(ChangeEmailRequest.objects.exists())

    def test_a_local_account_without_a_password_is_not_waived(self):
        # Its password was removed: it has no method, but it is not an account
        # that signs in through an identity provider.
        self.user.registration_method = "default"
        self.user.save()

        self.assert_challenge(self.change_email(), CHANGE_EMAIL, methods=[])
        self.assertFalse(ChangeEmailRequest.objects.exists())
        self.assertFalse(self.waived().exists())

    def test_a_passkey_that_cannot_be_used_here_is_not_waived(self):
        with enable_passkeys():
            enrol_passkey(self.user)
        PasskeyCredential.objects.filter(user=self.user).update(rp_id="old.example.org")

        with enable_passkeys():
            self.assert_challenge(self.change_email(), CHANGE_EMAIL, methods=[])
        self.assertFalse(self.waived().exists())

    @override_config(PAT_ENABLED=True)
    def test_a_personal_access_token_is_refused(self):
        _pat, token = create_pat(self.user, scopes=[PermissionEnum.LIST_ORDERS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = self.change_email()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(ChangeEmailRequest.objects.exists())
        self.assertFalse(self.waived().exists())

    def test_impersonation_is_refused(self):
        staff = make_user(is_staff=True)
        refresh_token(self.user)
        self.authenticate(staff, HTTP_X_IMPERSONATED_USER_UUID=self.user.uuid.hex)

        self.assertEqual(self.change_email().status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(ChangeEmailRequest.objects.exists())

    def test_staff_without_methods_still_need_a_grant_for_another_users_email(self):
        staff = make_user(is_staff=True)
        staff.set_unusable_password()
        staff.save()
        self.authenticate(staff)
        other = make_user()
        other_url = structure_factories.UserFactory.get_url(other)

        self.assert_challenge(
            self.client.post(
                other_url + "change_email/", {"email": "elsewhere@example.com"}
            ),
            CHANGE_EMAIL,
            methods=[],
        )
        self.assert_challenge(
            self.client.patch(other_url, {"email": "elsewhere@example.com"}),
            CHANGE_EMAIL,
            methods=[],
        )
        self.assertFalse(self.waived().exists())


class PendingEmailChangeMigrationTest(TokenHolderTest):
    """Requests opened before email changes needed re-authentication are
    dropped, or confirm_email would still apply them."""

    def test_pending_requests_are_deleted(self):
        self.user.create_request_for_update_email("elsewhere@example.com")
        migration = importlib.import_module(
            "waldur_core.core.migrations.0057_delete_pending_email_change_requests"
        )

        migration.delete_pending_requests(apps, None)

        self.assertFalse(ChangeEmailRequest.objects.exists())


class ImpersonationTest(TokenHolderTest):
    """Staff impersonating a user cannot make these changes for them."""

    def setUp(self):
        super().setUp()
        self.staff = make_user(is_staff=True)
        refresh_token(self.user)
        self.authenticate(self.staff, HTTP_X_IMPERSONATED_USER_UUID=self.user.uuid.hex)

    def test_guarded_action_is_refused(self):
        response = self.change_token_lifetime()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.user.refresh_from_db()
        self.assertEqual(self.user.token_lifetime, 3600)

    def test_a_valid_grant_does_not_help(self):
        _grant, secret = ReauthenticationGrant.issue(
            self.user, TOKEN_LIFETIME, "password", timedelta(minutes=5)
        )

        response = self.change_token_lifetime(**grant_header(secret))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIsNone(ReauthenticationGrant.objects.get().used_at)

    def test_no_grant_is_issued_with_the_password(self):
        response = self.reauthenticate(TOKEN_LIFETIME)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(ReauthenticationGrant.objects.exists())

    @enable_passkeys()
    def test_no_passkey_ceremony_is_begun(self):
        enrol_passkey(self.user)

        response = self.client.post(PASSKEY_BEGIN_URL, {"action": TOKEN_LIFETIME})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_refusal_is_recorded(self):
        self.change_token_lifetime()

        event = Event.objects.get(event_type="reauthentication_refused")
        self.assertIn("impersonat", event.message)


class PasswordLockoutTest(TokenHolderTest):
    """A token holder cannot guess the password here at will."""

    def test_a_wrong_password_issues_nothing(self):
        response = self.reauthenticate(CREATE_PAT, password="guess")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)
        self.assertFalse(ReauthenticationGrant.objects.exists())
        self.assertTrue(
            Event.objects.filter(event_type="reauthentication_failed").exists()
        )

    def test_wrong_passwords_lock_out_re_authentication_and_sign_in(self):
        for _ in range(4):
            self.reauthenticate(CREATE_PAT, password="guess")

        response = self.reauthenticate(CREATE_PAT)

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertFalse(ReauthenticationGrant.objects.exists())
        login = test.APIClient().post(
            LOGIN_URL, {"username": self.user.username, "password": PASSWORD}
        )
        self.assertEqual(login.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("token", login.data)
        self.assertTrue(Event.objects.filter(event_type="user_blocked").exists())

    def test_wrong_passwords_from_many_addresses_lock_the_user_out(self):
        # The login counter is per address; django-axes counts per username.
        for n in range(5):
            self.reauthenticate(
                CREATE_PAT, password="guess", REMOTE_ADDR=f"192.0.2.{n + 1}"
            )

        response = self.reauthenticate(CREATE_PAT, REMOTE_ADDR="198.51.100.7")

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        login = test.APIClient().post(
            LOGIN_URL,
            {"username": self.user.username, "password": PASSWORD},
            REMOTE_ADDR="203.0.113.9",
        )
        self.assertEqual(login.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_wrong_sign_in_passwords_lock_out_re_authentication(self):
        anonymous = test.APIClient()
        for _ in range(4):
            anonymous.post(
                LOGIN_URL, {"username": self.user.username, "password": "guess"}
            )

        response = self.reauthenticate(CREATE_PAT)

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


ONE_PER_MINUTE = {"reauthentication": "1/min"}


class ThrottledTest(TokenHolderTest):
    """A 429 says in its body when to retry: the portal, on another origin,
    cannot read Retry-After unless the API exposes it."""

    def assert_retry_after(self, response, seconds=None):
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("detail", response.data)
        retry_after = response.data["retry_after"]
        self.assertIsInstance(retry_after, int)
        if seconds is None:
            self.assertTrue(0 < retry_after <= 60, retry_after)
        else:
            self.assertEqual(retry_after, seconds)
        self.assertEqual(response["Retry-After"], str(retry_after))

    def test_lockout(self):
        for _ in range(4):
            self.reauthenticate(CREATE_PAT, password="guess")

        self.assert_retry_after(self.reauthenticate(CREATE_PAT), 600)

    @mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", ONE_PER_MINUTE)
    def test_rate_limit_of_the_password_endpoint(self):
        self.reauthenticate(CREATE_PAT, password="guess")

        self.assert_retry_after(self.reauthenticate(CREATE_PAT))

    @enable_passkeys()
    @mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", ONE_PER_MINUTE)
    def test_rate_limit_of_the_passkey_endpoints(self):
        enrol_passkey(self.user)
        begin = self.client.post(PASSKEY_BEGIN_URL, {"action": CREATE_PAT})
        self.assertEqual(begin.status_code, status.HTTP_200_OK)

        self.assert_retry_after(
            self.client.post(PASSKEY_BEGIN_URL, {"action": CREATE_PAT})
        )
        self.assert_retry_after(
            self.client.post(
                PASSKEY_FINISH_URL,
                {
                    "action": CREATE_PAT,
                    "ceremony": str(begin.data["ceremony"]),
                    "credential": {},
                },
                format="json",
            )
        )

    @mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", ONE_PER_MINUTE)
    def test_an_unknown_wait_is_the_whole_period(self):
        with self.assertRaises(reauthentication.ReauthenticationThrottled) as caught:
            reauthentication.ThrottledGrantEndpointMixin().throttled(None, None)

        self.assertEqual(caught.exception.detail["retry_after"], 60)
        self.assertEqual(caught.exception.wait, 60)


class EventsTest(TokenHolderTest):
    def events(self, event_type):
        return Event.objects.filter(
            event_type=event_type,
            id__in=Feed.objects.filter(
                content_type=ContentType.objects.get_for_model(User),
                object_id=self.user.id,
            ).values("event_id"),
        )

    def test_refusal_grant_and_use_are_recorded(self):
        self.create_pat()
        self.create_pat(**grant_header(self.grant_for(CREATE_PAT)))

        refused = self.events("reauthentication_refused").get()
        self.assertEqual(refused.context["reauthentication_action"], CREATE_PAT)
        granted = self.events("reauthentication_granted").get()
        self.assertEqual(granted.context["reauthentication_method"], "password")
        used = self.events("reauthentication_grant_used").get()
        self.assertEqual(used.context["reauthentication_action"], CREATE_PAT)

    def test_use_is_not_recorded_when_the_action_fails(self):
        pat, _ = create_pat(self.user, is_active=False)

        self.client.post(
            f"{PAT_URL}{pat.uuid.hex}/rotate/",
            **grant_header(self.grant_for(CREATE_PAT)),
        )

        self.assertFalse(self.events("reauthentication_grant_used").exists())
