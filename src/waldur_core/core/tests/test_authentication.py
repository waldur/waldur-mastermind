from unittest import mock

import httpx
import jwt
import respx
from axes.signals import user_locked_out
from constance.test.unittest import override_config
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.authentication import DEFAULT_TOKEN_UPDATE_INTERVAL, refresh_token
from waldur_core.core.models import User
from waldur_core.logging.models import Event

from . import helpers


class TokenAuthenticationTest(test.APITestCase):
    def setUp(self):
        self.username = "test"
        self.password = "secret"
        self.auth_url = reverse("auth-password")
        self.logout_url = reverse("auth-logout")
        self.test_url = "http://testserver/api/"
        self.user = User.objects.create_user(
            self.username, "admin@example.com", self.password
        )

    def tearDown(self):
        cache.clear()

    def test_user_can_authenticate_with_token(self):
        response = self.client.post(
            self.auth_url, data={"username": self.username, "password": self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        token = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token)
        response = self.client.get(self.test_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_can_logout(self):
        response = self.client.post(
            self.auth_url, data={"username": self.username, "password": self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        key = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION="Token " + key)
        token = Token.objects.get(key=key)
        response = self.client.post(self.logout_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertRaises(ObjectDoesNotExist, token.refresh_from_db)

    def test_token_expires_based_on_user_token_lifetime(self):
        user = User.objects.get(username=self.username)
        configured_token_lifetime = settings.WALDUR_CORE.get(
            "TOKEN_LIFETIME", timezone.timedelta(hours=1)
        )
        user_token_lifetime = configured_token_lifetime - timezone.timedelta(seconds=40)
        user.token_lifetime = user_token_lifetime.seconds
        user.save()

        response = self.client.post(
            self.auth_url, data={"username": self.username, "password": self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        token = response.data["token"]
        mocked_now = timezone.now() + user_token_lifetime
        with freeze_time(mocked_now):
            self.client.credentials(HTTP_AUTHORIZATION="Token " + token)
            response = self.client.get(self.test_url)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(response.data["detail"], "Token has expired.")

    def test_account_is_blocked_after_five_failed_attempts(self):
        for _ in range(5):
            response = self.client.post(
                self.auth_url, data={"username": self.username, "password": "WRONG"}
            )
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # this one should fail with a different error message
        self.client.post(
            self.auth_url, data={"username": self.username, "password": "WRONG"}
        )
        self.assertEqual(
            response.data["detail"], "Username is locked out. Try in 10 minutes."
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_blocked_event_is_emitted_once_when_lockout_threshold_is_reached(self):
        for _ in range(6):
            self.client.post(
                self.auth_url, data={"username": self.username, "password": "WRONG"}
            )

        events = Event.objects.filter(event_type="user_blocked")
        self.assertEqual(events.count(), 1)
        self.assertIn(self.username, events.first().message)

    def test_expired_token_is_recreated_on_successful_authentication(self):
        user = User.objects.get(username=self.username)
        self.assertIsNotNone(user.token_lifetime)
        response = self.client.post(
            self.auth_url, data={"username": self.username, "password": self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token1 = response.data["token"]

        mocked_now = timezone.now() + timezone.timedelta(seconds=user.token_lifetime)
        with freeze_time(mocked_now):
            response = self.client.post(
                self.auth_url,
                data={"username": self.username, "password": self.password},
            )
            token2 = response.data["token"]
            self.assertNotEqual(token1, token2)

    def test_refresh_token_positive(self):
        next_time = timezone.now() + timezone.timedelta(hours=1)
        token, _ = Token.objects.get_or_create(user=self.user)
        with freeze_time(next_time):
            token = refresh_token(self.user)
        self.assertGreater(token.created, timezone.now())

    def test_refresh_token_negative(self):
        next_time = timezone.now() + timezone.timedelta(seconds=10)
        token, _ = Token.objects.get_or_create(user=self.user)
        with freeze_time(next_time):
            token = refresh_token(self.user)
        self.assertLess(token.created, timezone.now())

    def test_refresh_token_tolerates_concurrent_rotation(self):
        """A concurrent request/task may rotate an expired token between our
        read and our write. Because Token.user is unique, the racing create()
        raises IntegrityError; refresh_token must recover by reusing the
        surviving token instead of bubbling up a 500 on the
        authtoken_token_user_id_key unique constraint."""
        self.user.token_lifetime = 10
        self.user.save()
        original, _ = Token.objects.get_or_create(user=self.user)

        expired_time = timezone.now() + timezone.timedelta(seconds=20)
        with freeze_time(expired_time):
            # Simulate losing the rotation race: our create() collides on the
            # unique user constraint. The atomic savepoint rolls back our
            # delete, so the surviving token is returned.
            with mock.patch.object(
                Token.objects,
                "create",
                side_effect=IntegrityError("duplicate key value"),
            ):
                token = refresh_token(self.user)

        self.assertEqual(Token.objects.filter(user=self.user).count(), 1)
        self.assertEqual(token.key, original.key)

    def test_token_not_refreshed_within_half_lifetime(self):
        """When user has token_lifetime set, the debounce interval is
        token_lifetime / 2. Token created timestamp should NOT be updated
        if less than half the lifetime has elapsed."""
        self.user.token_lifetime = 3600
        self.user.save()
        token = Token.objects.get(user=self.user)
        original_created = token.created

        # 1799 seconds is less than 3600 / 2 = 1800 debounce interval
        frozen_time = original_created + timezone.timedelta(seconds=1799)
        with freeze_time(frozen_time):
            refresh_token(self.user)

        token.refresh_from_db()
        self.assertEqual(token.created, original_created)

    def test_token_refreshed_after_half_lifetime(self):
        """When user has token_lifetime set, the debounce interval is
        token_lifetime / 2. Token created timestamp SHOULD be updated
        if more than half the lifetime has elapsed."""
        self.user.token_lifetime = 3600
        self.user.save()
        token = Token.objects.get(user=self.user)
        original_created = token.created

        # 1801 seconds is more than 3600 / 2 = 1800 debounce interval
        frozen_time = original_created + timezone.timedelta(seconds=1801)
        with freeze_time(frozen_time):
            refresh_token(self.user)

        token.refresh_from_db()
        self.assertGreater(token.created, original_created)

    def test_token_not_refreshed_within_default_interval_when_no_lifetime(self):
        """When user has no token_lifetime, the debounce interval falls back
        to DEFAULT_TOKEN_UPDATE_INTERVAL (600s). Token created timestamp
        should NOT be updated if less than 600 seconds have elapsed."""
        self.user.token_lifetime = None
        self.user.save()
        token = Token.objects.get(user=self.user)
        original_created = token.created

        # 599 seconds is less than the 600s default interval
        frozen_time = original_created + timezone.timedelta(seconds=599)
        with freeze_time(frozen_time):
            refresh_token(self.user)

        token.refresh_from_db()
        self.assertEqual(token.created, original_created)

    def test_token_refreshed_after_default_interval_when_no_lifetime(self):
        """When user has no token_lifetime, the debounce interval falls back
        to DEFAULT_TOKEN_UPDATE_INTERVAL (600s). Token created timestamp
        SHOULD be updated if more than 600 seconds have elapsed."""
        self.user.token_lifetime = None
        self.user.save()
        token = Token.objects.get(user=self.user)
        original_created = token.created

        # 601 seconds is more than the 600s default interval
        frozen_time = original_created + timezone.timedelta(
            seconds=DEFAULT_TOKEN_UPDATE_INTERVAL.total_seconds() + 1
        )
        with freeze_time(frozen_time):
            refresh_token(self.user)

        token.refresh_from_db()
        self.assertGreater(token.created, original_created)

    def test_token_never_expires_if_token_lifetime_is_none(self):
        user = User.objects.get(username=self.username)
        user.token_lifetime = None
        user.save()

        response = self.client.post(
            self.auth_url, data={"username": self.username, "password": self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        original_token = response.data["token"]

        year_ahead = timezone.now() + timezone.timedelta(days=365)
        with freeze_time(year_ahead):
            response = self.client.post(
                self.auth_url,
                data={"username": self.username, "password": self.password},
            )
            token_in_a_year = response.data["token"]
            self.assertEqual(original_token, token_in_a_year)

    def test_token_created_date_is_refreshed_even_if_token_lifetime_is_none(self):
        response = self.client.post(
            self.auth_url, data={"username": self.username, "password": self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user = User.objects.get(username=self.username)
        original_token_lifetime = user.token_lifetime
        original_created_value = user.auth_token.created
        user.token_lifetime = None
        user.save()

        last_refresh_time = timezone.now() + timezone.timedelta(
            seconds=original_token_lifetime
        )
        with freeze_time(last_refresh_time):
            response = self.client.post(
                self.auth_url,
                data={"username": self.username, "password": self.password},
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            token = response.data["token"]

        user.auth_token.refresh_from_db()
        self.assertTrue(user.auth_token.created > original_created_value)

        user.token_lifetime = original_token_lifetime
        user.save()
        with freeze_time(last_refresh_time):
            self.client.credentials(HTTP_AUTHORIZATION="Token " + token)
            response = self.client.get(self.test_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    @helpers.override_waldur_core_settings(AUTHENTICATION_METHODS=[])
    def test_authentication_fails_if_local_signin_is_disabled(self):
        response = self.client.post(
            self.auth_url, data={"username": self.username, "password": self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(b"Authentication method is disabled." in response.content)


class AxesLockoutEventTest(test.APITestCase):
    def tearDown(self):
        cache.clear()

    def test_user_blocked_event_is_emitted_on_axes_lockout(self):
        user_locked_out.send(
            "axes",
            request=None,
            username="attacker",
            ip_address="203.0.113.7",
        )

        events = Event.objects.filter(event_type="user_blocked")
        self.assertEqual(events.count(), 1)
        message = events.first().message
        self.assertIn("attacker", message)
        self.assertIn("203.0.113.7", message)

    def test_repeated_axes_lockout_signals_emit_a_single_event(self):
        # django-axes re-sends user_locked_out on every attempt made while the
        # lockout is already in force; only the first should be logged.
        for _ in range(5):
            user_locked_out.send(
                "axes",
                request=None,
                username="attacker",
                ip_address="203.0.113.7",
            )

        self.assertEqual(Event.objects.filter(event_type="user_blocked").count(), 1)


VALID_JWT_PAYLOAD = {
    "exp": 9999999999,  # Far future
    "username": "test_user",
    "sub": "1234567890",
}

VALID_JWT_TOKEN = jwt.encode(VALID_JWT_PAYLOAD, "test_secret")


@override_config(
    OIDC_INTROSPECTION_URL="http://oidc.example.com/introspect",
    OIDC_CLIENT_ID="test-client",
    OIDC_CLIENT_SECRET="test-secret",
    OIDC_USER_FIELD="username",
)
class OIDCAuthenticationTest(test.APITestCase):
    def tearDown(self):
        cache.clear()

    @respx.mock
    def test_successful_authentication(self):
        """Test successful authentication with valid token and introspection response"""

        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": True, "username": "test_user"}
            )
        )

        response = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Bearer {VALID_JWT_TOKEN}",
        )
        self.assertEqual(response.data["username"], "test_user")

    @respx.mock
    def test_user_is_not_active(self):
        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": False, "username": "test_user"}
            )
        )

        response = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Bearer {VALID_JWT_TOKEN}",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @respx.mock
    def test_expired_token(self):
        """Test that authentication fails with expired JWT token"""
        expired_payload = {
            "exp": 1500000000,  # Past timestamp
            "username": "test_user",
        }
        expired_token = jwt.encode(expired_payload, "test_secret")

        response = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Bearer {expired_token}",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["detail"], "Token has expired.")

    @respx.mock
    def test_existing_inactive_user_is_rejected(self):
        """An existing deactivated user must be rejected, not crash on re-creation.

        The default User.objects manager filters out inactive users, so a naive
        get_or_create would miss the existing row and then collide on the unique
        username constraint, producing a 500. Authentication must return 401.
        """
        username = "deactivated_user"
        User.objects.create_user(username=username, is_active=False)

        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": True, "username": username}
            )
        )

        response = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Bearer {VALID_JWT_TOKEN}",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        # No duplicate row was created.
        self.assertEqual(User.all_objects.filter(username=username).count(), 1)

    @respx.mock
    def test_user_created_if_not_exists(self):
        """Test that a new user is created when they don't exist in the system"""
        non_existent_username = "new_test_user"

        # Create payload with new username
        payload = {
            "exp": 9999999999,
            "username": non_existent_username,
            "sub": "0987654321",
        }
        token = jwt.encode(payload, "test_secret")

        respx.post("http://oidc.example.com/introspect").mock(
            return_value=httpx.Response(
                200, json={"active": True, "username": non_existent_username}
            )
        )

        # Verify user doesn't exist before request
        self.assertFalse(User.objects.filter(username=non_existent_username).exists())

        response = self.client.get(
            "/api/users/me/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        # Verify response and user creation
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], non_existent_username)
        self.assertTrue(User.objects.filter(username=non_existent_username).exists())
