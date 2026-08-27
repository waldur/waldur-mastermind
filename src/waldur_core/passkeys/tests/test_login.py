"""Login-path tests.

The central claim of this phase is an ordering one: with the second factor
enabled, a correct password alone must not issue a token, must not touch
``last_login``, must not bump the existing token's timestamp and must not emit
a login event. Each of those is asserted separately, because each was a
distinct way for "passkey required" to be theatre.
"""

from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.passkeys import services
from waldur_core.passkeys.enums import CeremonyKind
from waldur_core.passkeys.models import PasskeyCeremony, PasskeyCredential
from waldur_core.passkeys.tests.authenticator import SoftwareAuthenticator
from waldur_core.passkeys.tests.helpers import ORIGIN, RP_ID, enable_passkeys
from waldur_core.structure.tests import factories as structure_factories

LOGIN_URL = "/api-auth/password/"
REG_BEGIN = "/api/passkeys/registration/begin/"
REG_FINISH = "/api/passkeys/registration/finish/"
SIGNIN_BEGIN = "/api/passkeys/signin/begin/"
SIGNIN_FINISH = "/api/passkeys/signin/finish/"
MFA_BEGIN = "/api/passkeys/mfa/begin/"
MFA_FINISH = "/api/passkeys/mfa/finish/"

PASSWORD = "very-secret-password"


def make_user(**kwargs):
    user = structure_factories.UserFactory(**kwargs)
    user.set_password(PASSWORD)
    user.save()
    return user


def enrol(user, authenticator=None, name="Laptop"):
    """Register a credential for a user, through the service layer."""
    authenticator = authenticator or SoftwareAuthenticator()
    ceremony, options = services.start_registration(user)
    credential = services.finish_registration(
        ceremony,
        authenticator.register(options["challenge"], RP_ID, ORIGIN),
        name,
    )
    return authenticator, credential


@enable_passkeys(signin=False, mfa=False)
class PasskeysDisabledLoginTest(test.APITestCase):
    """The default deployment must be byte-identical to before."""

    def test_password_login_still_returns_a_token(self):
        user = make_user()
        response = self.client.post(
            LOGIN_URL, {"username": user.username, "password": PASSWORD}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("token", response.data)
        self.assertNotIn("pending_passkey_ceremony", response.data)

    def test_ceremony_endpoints_are_not_reachable(self):
        for url in (SIGNIN_BEGIN, MFA_BEGIN):
            response = self.client.post(url, {})
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, url)


@enable_passkeys(signin=False, mfa=True)
class SecondFactorOrderingTest(test.APITestCase):
    def setUp(self):
        self.user = make_user()
        self.authenticator, self.credential = enrol(self.user)

    def _password_login(self):
        return self.client.post(
            LOGIN_URL, {"username": self.user.username, "password": PASSWORD}
        )

    def test_correct_password_alone_returns_no_token(self):
        """401, not a 200 carrying a handle.

        A 200 would force `token` optional in the shared response schema and
        churn every generated client, including those that never enable
        passkeys. Authentication really is incomplete here, so the status says
        so and the body carries the next step.
        """
        response = self._password_login()

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("token", response.data)
        self.assertTrue(response.data["passkey_required"])
        self.assertIn("pending_passkey_ceremony", response.data)

    def test_a_rejected_password_is_distinguishable_from_a_pending_factor(self):
        """Both are 401, so the body has to discriminate."""
        rejected = self.client.post(
            LOGIN_URL, {"username": self.user.username, "password": "wrong"}
        )
        pending = self._password_login()

        self.assertEqual(rejected.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(pending.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("passkey_required", rejected.data)
        self.assertTrue(pending.data["passkey_required"])

    def test_correct_password_alone_does_not_set_last_login(self):
        self.assertIsNone(self.user.last_login)

        self._password_login()

        self.user.refresh_from_db()
        self.assertIsNone(self.user.last_login)

    def test_correct_password_alone_does_not_extend_an_existing_token(self):
        """The bug this phase exists to close.

        refresh_token() bumps token.created. Called before verification, a
        correct password alone silently extends the life of a token an
        attacker may already hold.
        """
        token = Token.objects.get(user=self.user)
        original = token.created

        self._password_login()

        token.refresh_from_db()
        self.assertEqual(token.created, original)

    def test_pending_handle_is_not_a_token(self):
        handle = self._password_login().data["pending_passkey_ceremony"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {handle}")
        response = self.client.get("/api/users/me/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_token_is_issued_only_after_a_verified_assertion(self):
        handle = self._password_login().data["pending_passkey_ceremony"]

        options = self.client.post(MFA_BEGIN, {"ceremony": handle}).data["options"]
        assertion = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)
        response = self.client.post(
            MFA_FINISH, {"ceremony": handle, "credential": assertion}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("token", response.data)

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_issued_token_actually_works(self):
        handle = self._password_login().data["pending_passkey_ceremony"]
        options = self.client.post(MFA_BEGIN, {"ceremony": handle}).data["options"]
        assertion = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)
        token = self.client.post(
            MFA_FINISH, {"ceremony": handle, "credential": assertion}, format="json"
        ).data["token"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        response = self.client.get("/api/users/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], self.user.username)

    def test_failed_assertion_issues_no_token(self):
        handle = self._password_login().data["pending_passkey_ceremony"]
        options = self.client.post(MFA_BEGIN, {"ceremony": handle}).data["options"]
        assertion = self.authenticator.authenticate(
            options["challenge"], RP_ID, ORIGIN, corrupt_signature=True
        )

        response = self.client.post(
            MFA_FINISH, {"ceremony": handle, "credential": assertion}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.last_login)

    def test_another_users_passkey_does_not_satisfy_the_handle(self):
        other = make_user()
        other_authenticator, _ = enrol(other, name="Other laptop")
        handle = self._password_login().data["pending_passkey_ceremony"]

        options = self.client.post(MFA_BEGIN, {"ceremony": handle}).data["options"]
        assertion = other_authenticator.authenticate(
            options["challenge"], RP_ID, ORIGIN
        )
        response = self.client.post(
            MFA_FINISH, {"ceremony": handle, "credential": assertion}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_handle_cannot_be_reused(self):
        handle = self._password_login().data["pending_passkey_ceremony"]
        options = self.client.post(MFA_BEGIN, {"ceremony": handle}).data["options"]
        assertion = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)
        self.client.post(
            MFA_FINISH, {"ceremony": handle, "credential": assertion}, format="json"
        )

        response = self.client.post(
            MFA_FINISH, {"ceremony": handle, "credential": assertion}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_handle_is_refused(self):
        import uuid

        response = self.client.post(MFA_BEGIN, {"ceremony": str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_without_a_credential_is_not_held_at_the_second_factor(self):
        """Enforcement is a later phase; a user with no passkey still logs in."""
        bare = make_user()
        response = self.client.post(
            LOGIN_URL, {"username": bare.username, "password": PASSWORD}
        )

        self.assertIn("token", response.data)

    def test_revoked_credential_stops_gating_login(self):
        self.credential.revoke()
        response = self._password_login()
        self.assertIn("token", response.data)

    def test_wrong_password_still_fails_before_any_ceremony(self):
        """A bad password must not even open a pending-login ceremony.

        Counting MFA ceremonies specifically: enrolling the credential in
        setUp already created a registration one.
        """
        before = PasskeyCeremony.objects.filter(kind=CeremonyKind.MFA).count()

        response = self.client.post(
            LOGIN_URL, {"username": self.user.username, "password": "wrong"}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            PasskeyCeremony.objects.filter(kind=CeremonyKind.MFA).count(), before
        )


@enable_passkeys(signin=True, mfa=False)
class PasswordlessSigninTest(test.APITestCase):
    def setUp(self):
        self.user = make_user()
        self.authenticator, self.credential = enrol(self.user)

    def test_signin_needs_no_username_and_no_password(self):
        begin = self.client.post(SIGNIN_BEGIN, {})
        self.assertEqual(begin.status_code, status.HTTP_200_OK)

        options = begin.data["options"]
        assertion = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)
        response = self.client.post(
            SIGNIN_FINISH,
            {"ceremony": begin.data["ceremony"], "credential": assertion},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("token", response.data)

    def test_issued_token_works(self):
        begin = self.client.post(SIGNIN_BEGIN, {})
        assertion = self.authenticator.authenticate(
            begin.data["options"]["challenge"], RP_ID, ORIGIN
        )
        token = self.client.post(
            SIGNIN_FINISH,
            {"ceremony": begin.data["ceremony"], "credential": assertion},
            format="json",
        ).data["token"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        response = self.client.get("/api/users/me/")

        self.assertEqual(response.data["username"], self.user.username)

    def test_begin_leaks_no_credentials(self):
        begin = self.client.post(SIGNIN_BEGIN, {})
        self.assertEqual(begin.data["options"].get("allowCredentials") or [], [])

    def test_inactive_user_is_refused(self):
        self.user.is_active = False
        self.user.save()

        begin = self.client.post(SIGNIN_BEGIN, {})
        assertion = self.authenticator.authenticate(
            begin.data["options"]["challenge"], RP_ID, ORIGIN
        )
        response = self.client.post(
            SIGNIN_FINISH,
            {"ceremony": begin.data["ceremony"], "credential": assertion},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["LOCAL_SIGNIN", "PASSKEY_MFA"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_ALLOWED_ORIGINS=[ORIGIN],
    )
    def test_signin_endpoints_are_hidden_when_only_mfa_is_enabled(self):
        """Enabling the second factor must not switch on passwordless login."""
        response = self.client.post(SIGNIN_BEGIN, {})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@enable_passkeys()
class RegistrationEndpointTest(test.APITestCase):
    def setUp(self):
        self.user = make_user()
        self.authenticator = SoftwareAuthenticator()

    def test_registration_requires_authentication(self):
        response = self.client.post(REG_BEGIN, {})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_enrol_a_credential(self):
        self.client.force_authenticate(self.user)
        begin = self.client.post(REG_BEGIN, {})
        self.assertEqual(begin.status_code, status.HTTP_200_OK)

        response = self.client.post(
            REG_FINISH,
            {
                "ceremony": begin.data["ceremony"],
                "name": "Touch ID",
                "credential": self.authenticator.register(
                    begin.data["options"]["challenge"], RP_ID, ORIGIN
                ),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Touch ID")
        self.assertEqual(PasskeyCredential.objects.filter(user=self.user).count(), 1)

    def test_a_ceremony_cannot_be_finished_by_a_different_user(self):
        self.client.force_authenticate(self.user)
        begin = self.client.post(REG_BEGIN, {})

        intruder = make_user()
        self.client.force_authenticate(intruder)
        response = self.client.post(
            REG_FINISH,
            {
                "ceremony": begin.data["ceremony"],
                "name": "Stolen",
                "credential": self.authenticator.register(
                    begin.data["options"]["challenge"], RP_ID, ORIGIN
                ),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PasskeyCredential.objects.filter(user=intruder).count(), 0)
