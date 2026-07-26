"""
Behaviour tests for OIDC adoption of pre-existing local accounts (SEC-C3).

`OIDCAuthentication.authenticate` used to call
    User.objects.get_or_create(username=user_identifier)
which silently matched any pre-existing local user — including high-
privilege staff accounts — whenever the IdP introspection returned an
`active` token whose `username` claim collided with that local username.

We keep that adoption behaviour *by design* (so existing accounts keep
working across the OIDC rollout rather than being locked out), but make
it fully auditable: an OIDC token that adopts a non-OIDC account re-tags
the account with `registration_method="oidc"`, emits a WARNING log, and
records a django-reversion snapshot of the pre-adoption state so the
change can be reviewed and reverted if it turns out to be a takeover.
"""

import httpx
import jwt
import respx
from constance.test.unittest import override_config
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase
from reversion.models import Version

from waldur_core.core.authentication import OIDCAuthentication
from waldur_core.core.models import User

VALID_PAYLOAD = {"exp": 9999999999, "username": "victim", "sub": "att-sub"}
VALID_TOKEN = jwt.encode(VALID_PAYLOAD, "any-key")


@override_config(
    OIDC_INTROSPECTION_URL="http://oidc.example.com/introspect",
    OIDC_CLIENT_ID="test-client",
    OIDC_CLIENT_SECRET="test-secret",
    OIDC_USER_FIELD="username",
)
class OidcAccountAdoptionTest(APITestCase):
    def tearDown(self):
        cache.clear()

    @respx.mock
    def test_oidc_adopts_pre_existing_local_staff_account_with_audit_trail(self):
        # Pre-existing local (registration_method="default") staff user.
        victim = User.objects.create(
            username="victim",
            email="victim@waldur.example",
            is_staff=True,
            is_active=True,
            registration_method="default",
        )

        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": True, "username": "victim"}
            )
        )

        with self.assertLogs(
            "waldur_core.core.authentication", level="WARNING"
        ) as logs:
            response = self.client.get(
                "/api/users/me/", HTTP_AUTHORIZATION=f"Bearer {VALID_TOKEN}"
            )

        # Adoption is allowed by design.
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # The account is re-tagged so subsequent logins bind cleanly.
        victim.refresh_from_db()
        self.assertEqual(
            victim.registration_method,
            OIDCAuthentication.REGISTRATION_METHOD,
        )

        # A WARNING was emitted naming the adoption and the token subject.
        self.assertTrue(
            any("adopting pre-existing local account" in m for m in logs.output),
            logs.output,
        )
        self.assertTrue(any("att-sub" in m for m in logs.output), logs.output)

        # A reversion snapshot of the pre-adoption state exists and is
        # revertible back to the original registration_method.
        versions = Version.objects.get_for_object(victim)
        self.assertTrue(versions.exists())
        comments = [v.revision.get_comment() for v in versions]
        self.assertTrue(
            any("Pre-OIDC-adoption snapshot" in c for c in comments), comments
        )
        snapshot = versions.filter(
            revision__comment__contains="Pre-OIDC-adoption snapshot"
        ).first()
        self.assertEqual(
            snapshot.field_dict["registration_method"],
            "default",
            "The snapshot must preserve the original registration_method so a "
            "suspected takeover can be reverted.",
        )

    @respx.mock
    def test_first_oidc_login_provisions_with_oidc_registration_method(self):
        self.assertFalse(User.objects.filter(username="newcomer").exists())

        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": True, "username": "newcomer"}
            )
        )
        token = jwt.encode({"exp": 9999999999, "username": "newcomer"}, "k")

        response = self.client.get(
            "/api/users/me/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_user = User.objects.get(username="newcomer")
        self.assertEqual(
            new_user.registration_method,
            OIDCAuthentication.REGISTRATION_METHOD,
            "Newly provisioned OIDC user should be marked with the OIDC "
            "registration method so subsequent logins re-bind safely.",
        )

    @respx.mock
    def test_existing_oidc_user_can_log_in_again_without_new_revision(self):
        user = User.objects.create(
            username="legit-oidc",
            email="oidc@waldur.example",
            registration_method=OIDCAuthentication.REGISTRATION_METHOD,
            is_active=True,
        )
        versions_before = Version.objects.get_for_object(user).count()

        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": True, "username": "legit-oidc"}
            )
        )
        token = jwt.encode({"exp": 9999999999, "username": "legit-oidc"}, "k")
        response = self.client.get(
            "/api/users/me/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            "An OIDC-provisioned user must still be able to authenticate "
            "with a fresh token.",
        )
        # Already OIDC-tagged: no adoption, so no extra revision noise.
        self.assertEqual(Version.objects.get_for_object(user).count(), versions_before)

    @respx.mock
    def test_inactive_user_is_rejected_and_not_adopted(self):
        user = User.objects.create(
            username="disabled-local",
            registration_method="default",
            is_active=False,
        )

        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": True, "username": "disabled-local"}
            )
        )
        token = jwt.encode({"exp": 9999999999, "username": "disabled-local"}, "k")
        response = self.client.get(
            "/api/users/me/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        # Rejected before any adoption/re-tagging happened.
        user.refresh_from_db()
        self.assertEqual(user.registration_method, "default")
