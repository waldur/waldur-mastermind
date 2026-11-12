"""Failed password attempts lock the account, and are limited per address.

A shared address -- a classroom, a university network, a company proxy, or
the ingress in front of the API -- must not let one user's typos lock out
everybody else. Guessing from one address is slowed by a limit on failed
attempts across usernames instead.
"""

from datetime import timedelta

from axes.models import AccessAttempt
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.core.utils import get_proxied_client_ip
from waldur_core.structure.tests import factories as structure_factories

LOGIN_URL = "/api-auth/password/"
REAUTHENTICATION_URL = "/api/reauthentication/password/"
ADMIN_LOGIN_URL = "/admin/login/"
PASSWORD = "Correct-Horse-Battery-9"
FAST_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# The ingress, which every request behind it comes from.
INGRESS = "10.42.0.7"


def make_user():
    user = structure_factories.UserFactory(agreement_date=timezone.now())
    user.set_password(PASSWORD)
    user.save()
    return user


@override_settings(PASSWORD_HASHERS=FAST_HASHER)
class FailureLimitTestCase(test.APITestCase):
    def setUp(self):
        # The login counter and the address counter live in the cache.
        cache.clear()

    def tearDown(self):
        cache.clear()

    def sign_in(self, username, password, **meta):
        return test.APIClient().post(
            LOGIN_URL, {"username": username, "password": password}, **meta
        )


class SharedAddressTest(FailureLimitTestCase):
    def test_one_users_failures_do_not_lock_another_user_out(self):
        locked, typist, other = make_user(), make_user(), make_user()
        for _ in range(5):
            self.sign_in(locked.username, "guess", REMOTE_ADDR="192.0.2.10")
        for _ in range(2):
            self.sign_in(typist.username, "typo", REMOTE_ADDR="192.0.2.10")

        self.assertEqual(
            self.sign_in(
                locked.username, PASSWORD, REMOTE_ADDR="192.0.2.10"
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        for user in (typist, other):
            response = self.sign_in(user.username, PASSWORD, REMOTE_ADDR="192.0.2.10")
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_users_behind_the_ingress_do_not_lock_each_other_out(self):
        locked, typist, other = make_user(), make_user(), make_user()
        for _ in range(3):
            self.sign_in(
                locked.username,
                "guess",
                REMOTE_ADDR=INGRESS,
                HTTP_X_FORWARDED_FOR="203.0.113.5",
            )
        for _ in range(2):
            self.sign_in(
                typist.username,
                "typo",
                REMOTE_ADDR=INGRESS,
                HTTP_X_FORWARDED_FOR="198.51.100.7",
            )

        response = self.sign_in(
            other.username,
            PASSWORD,
            REMOTE_ADDR=INGRESS,
            HTTP_X_FORWARDED_FOR="198.51.100.8",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class AccountLockoutTest(FailureLimitTestCase):
    def test_a_username_is_locked_after_five_failures_from_any_addresses(self):
        user = make_user()
        for n in range(5):
            self.sign_in(user.username, "guess", REMOTE_ADDR=f"192.0.2.{n + 1}")

        response = self.sign_in(user.username, PASSWORD, REMOTE_ADDR="198.51.100.9")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("token", response.data)


class AddressLimitTest(FailureLimitTestCase):
    """Fifty failures an hour from one address, across usernames, by default."""

    def fail_from(self, address, times):
        for n in range(times):
            self.sign_in(f"nobody-{n}", "guess", REMOTE_ADDR=address)

    def test_the_default_limit_trips_with_a_429(self):
        user = make_user()
        self.fail_from("192.0.2.20", 50)

        response = self.sign_in(user.username, PASSWORD, REMOTE_ADDR="192.0.2.20")

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertGreater(response.data["retry_after"], 0)
        self.assertEqual(response["Retry-After"], str(response.data["retry_after"]))
        other_address = self.sign_in(user.username, PASSWORD, REMOTE_ADDR="192.0.2.21")
        self.assertEqual(other_address.status_code, status.HTTP_200_OK)


@override_config(PASSWORD_FAILURES_PER_ADDRESS_LIMIT=3)
class ConfiguredAddressLimitTest(FailureLimitTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def fail_from(self, address, times, **meta):
        for n in range(times):
            response = self.sign_in(f"nobody-{n}", "guess", REMOTE_ADDR=address, **meta)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def assert_limited(self, response):
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        # The cool-off, 10 minutes by default.
        self.assertGreater(response.data["retry_after"], 590)
        self.assertLessEqual(response.data["retry_after"], 600)
        self.assertEqual(response["Retry-After"], str(response.data["retry_after"]))

    def test_failures_across_usernames_limit_the_address(self):
        self.fail_from("192.0.2.30", 3)

        self.assert_limited(
            self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
        )
        response = self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.31")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_successful_attempts_do_not_count(self):
        self.fail_from("192.0.2.30", 2)
        for _ in range(5):
            response = self.sign_in(
                self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.sign_in("nobody-x", "guess", REMOTE_ADDR="192.0.2.30")

        self.assert_limited(
            self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
        )

    def test_after_the_cool_off_the_address_is_counted_afresh(self):
        start = timezone.now()
        with freeze_time(start):
            self.fail_from("192.0.2.30", 3)
            wait = self.sign_in(
                self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30"
            ).data["retry_after"]

        with freeze_time(start + timedelta(seconds=wait)):
            response = self.sign_in(
                self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            # Two failures after the cool-off are two, not five.
            self.fail_from("192.0.2.30", 2)
            response = self.sign_in(
                self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.fail_from("192.0.2.30", 1)
            self.assert_limited(
                self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
            )

    @override_config(PASSWORD_FAILURES_PER_ADDRESS_LIMIT=50)
    def test_a_lowered_limit_applies_to_an_address_already_above_it(self):
        # An admin who tightens the limit during an attack sees it at once.
        self.fail_from("192.0.2.30", 5)

        with override_config(PASSWORD_FAILURES_PER_ADDRESS_LIMIT=3):
            self.sign_in("nobody-x", "guess", REMOTE_ADDR="192.0.2.30")
            self.assert_limited(
                self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
            )

    @override_config(PASSWORD_FAILURES_PER_ADDRESS_COOLOFF_MINUTES=2)
    def test_the_cool_off_is_configured(self):
        self.fail_from("192.0.2.30", 3)

        response = self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertTrue(110 < response.data["retry_after"] <= 120)

    def test_failures_spread_beyond_the_window_do_not_add_up(self):
        start = timezone.now()
        with freeze_time(start):
            self.fail_from("192.0.2.30", 2)
        with freeze_time(start + timedelta(minutes=61)):
            self.fail_from("192.0.2.30", 2)
            response = self.sign_in(
                self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_ipv6_addresses_count_per_64(self):
        for n in range(3):
            self.sign_in(f"nobody-{n}", "guess", REMOTE_ADDR=f"2001:db8:1:2::{n + 1}")

        self.assert_limited(
            self.sign_in(
                self.user.username, PASSWORD, REMOTE_ADDR="2001:db8:1:2:ffff::9"
            )
        )
        response = self.sign_in(
            self.user.username, PASSWORD, REMOTE_ADDR="2001:db8:1:3::1"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(PASSWORD_FAILURES_PER_ADDRESS_LIMIT=0)
    def test_zero_turns_the_limit_off(self):
        self.fail_from("192.0.2.30", 4)

        response = self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_re_authentication_counts_and_is_limited(self):
        api = test.APIClient()
        token, _ = Token.objects.get_or_create(user=self.user)
        api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        def reauthenticate(password):
            return api.post(
                REAUTHENTICATION_URL,
                {"action": "change_email", "password": password},
                REMOTE_ADDR="192.0.2.30",
            )

        self.fail_from("192.0.2.30", 1)
        for _ in range(2):
            self.assertEqual(
                reauthenticate("guess").status_code, status.HTTP_400_BAD_REQUEST
            )

        self.assert_limited(reauthenticate(PASSWORD))
        self.assert_limited(
            self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
        )

    def test_admin_login_counts_and_is_limited(self):
        staff = make_user()
        staff.is_staff = True
        staff.save()
        browser = Client(REMOTE_ADDR="192.0.2.30")
        for _ in range(3):
            response = browser.post(
                ADMIN_LOGIN_URL, {"username": staff.username, "password": "guess"}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = browser.post(
            ADMIN_LOGIN_URL, {"username": staff.username, "password": PASSWORD}
        )

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertGreater(int(response["Retry-After"]), 0)
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assert_limited(
            self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
        )

    def test_a_correct_admin_password_refused_for_another_reason_does_not_count(self):
        # Not staff: the password is right, the account may not use the admin.
        browser = Client(REMOTE_ADDR="192.0.2.30")
        for _ in range(4):
            browser.post(
                ADMIN_LOGIN_URL,
                {"username": self.user.username, "password": PASSWORD},
            )

        response = self.sign_in(self.user.username, PASSWORD, REMOTE_ADDR="192.0.2.30")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@override_config(PASSWORD_FAILURES_PER_ADDRESS_LIMIT=3)
class ForwardedForTest(FailureLimitTestCase):
    """Behind the ingress, the client is the first X-Forwarded-For hop, as
    axes records it, and not the ingress every request comes from."""

    def test_axes_records_the_forwarded_address(self):
        self.sign_in(
            "nobody", "guess", REMOTE_ADDR=INGRESS, HTTP_X_FORWARDED_FOR="203.0.113.5"
        )

        attempt = AccessAttempt.objects.get(username="nobody")
        self.assertEqual(attempt.ip_address, "203.0.113.5")

    def test_the_limit_counts_each_client_behind_the_ingress(self):
        user = make_user()
        for n in range(3):
            self.sign_in(
                f"nobody-{n}",
                "guess",
                REMOTE_ADDR=INGRESS,
                HTTP_X_FORWARDED_FOR="203.0.113.5",
            )

        limited = self.sign_in(
            user.username,
            PASSWORD,
            REMOTE_ADDR=INGRESS,
            HTTP_X_FORWARDED_FOR="203.0.113.5",
        )
        self.assertEqual(limited.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        other_client = self.sign_in(
            user.username,
            PASSWORD,
            REMOTE_ADDR=INGRESS,
            HTTP_X_FORWARDED_FOR="198.51.100.7",
        )
        self.assertEqual(other_client.status_code, status.HTTP_200_OK)


class MalformedForwardedForTest(FailureLimitTestCase):
    """A forwarded address that is not an address falls back to the peer; it
    must never turn off the lockout by failing the request before anything is
    counted."""

    BAD_HEADERS = (
        "203.0.113.5:4711",
        "unknown",
        "",
        "203.0.113." + "9" * 5000,
    )

    def test_the_account_is_still_locked(self):
        for header in self.BAD_HEADERS:
            with self.subTest(header=header[:30]):
                cache.clear()
                user = make_user()
                for _ in range(5):
                    response = self.sign_in(
                        user.username,
                        "guess",
                        REMOTE_ADDR=INGRESS,
                        HTTP_X_FORWARDED_FOR=header,
                    )
                    self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
                self.assertTrue(AccessAttempt.objects.filter(username=user.username))

                response = self.sign_in(
                    user.username,
                    PASSWORD,
                    REMOTE_ADDR=INGRESS,
                    HTTP_X_FORWARDED_FOR=header,
                )
                self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
                self.assertNotIn("token", response.data)

    def test_re_authentication_still_counts(self):
        user = make_user()
        api = test.APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        response = api.post(
            REAUTHENTICATION_URL,
            {"action": "change_email", "password": "guess"},
            REMOTE_ADDR=INGRESS,
            HTTP_X_FORWARDED_FOR="unknown",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(AccessAttempt.objects.filter(username=user.username))

    def test_admin_login_works_and_counts(self):
        staff = make_user()
        staff.is_staff = True
        staff.save()
        browser = Client(REMOTE_ADDR=INGRESS, HTTP_X_FORWARDED_FOR="203.0.113.5:4711")

        wrong = browser.post(
            ADMIN_LOGIN_URL, {"username": staff.username, "password": "guess"}
        )
        right = browser.post(
            ADMIN_LOGIN_URL, {"username": staff.username, "password": PASSWORD}
        )

        self.assertEqual(wrong.status_code, status.HTTP_200_OK)
        self.assertEqual(right.status_code, status.HTTP_302_FOUND)
        self.assertEqual(
            AccessAttempt.objects.get(username=staff.username).ip_address,
            "203.0.113.5",
        )


@override_config(PASSWORD_FAILURES_PER_ADDRESS_LIMIT=3)
class AppendingProxyTest(FailureLimitTestCase):
    """Proxies such as HAProxy with option forwardfor, or Envoy, append the
    client to whatever X-Forwarded-For the client sent. The entry they added
    is the last one; anything before it is the client's own text."""

    VICTIM = "198.51.100.20"
    ATTACKER = "203.0.113.66"

    def setUp(self):
        super().setUp()
        self.user = make_user()

    def fail_with(self, forwarded_for, n):
        return self.sign_in(
            f"nobody-{n}",
            "guess",
            REMOTE_ADDR=INGRESS,
            HTTP_X_FORWARDED_FOR=forwarded_for,
        )

    def sign_in_from(self, forwarded_for):
        return self.sign_in(
            self.user.username,
            PASSWORD,
            REMOTE_ADDR=INGRESS,
            HTTP_X_FORWARDED_FOR=forwarded_for,
        )

    def test_a_client_cannot_put_another_address_over_the_limit(self):
        for n in range(3):
            self.fail_with(f"{self.VICTIM}, {self.ATTACKER}", n)

        response = self.sign_in_from(self.VICTIM)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_client_cannot_escape_the_limit_by_varying_the_first_hop(self):
        for n in range(3):
            self.fail_with(f"192.0.2.{n + 1}, {self.ATTACKER}", n)

        response = self.sign_in_from(f"192.0.2.99, {self.ATTACKER}")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_waldur_core_settings(TRUSTED_PROXY_COUNT=2)
    def test_a_load_balancer_in_front_of_the_ingress(self):
        # The client, then the load balancer, as the ingress appends them.
        load_balancer = "10.0.0.2"
        for n in range(3):
            self.fail_with(f"{self.VICTIM}, {self.ATTACKER}, {load_balancer}", n)

        self.assertEqual(
            self.sign_in_from(f"{self.ATTACKER}, {load_balancer}").status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
        self.assertEqual(
            self.sign_in_from(f"{self.VICTIM}, {load_balancer}").status_code,
            status.HTTP_200_OK,
        )


class ProxiedClientAddressTest(test.APITestCase):
    """The resolver behind axes and the address limit."""

    factory = test.APIRequestFactory()

    def resolve(self, forwarded_for=None, remote_addr=INGRESS):
        meta = {"REMOTE_ADDR": remote_addr}
        if forwarded_for is not None:
            meta["HTTP_X_FORWARDED_FOR"] = forwarded_for
        return get_proxied_client_ip(self.factory.get("/", **meta))

    def test_the_entry_the_proxy_added(self):
        self.assertEqual(self.resolve("203.0.113.5"), "203.0.113.5")
        self.assertEqual(self.resolve("198.51.100.1, 203.0.113.5"), "203.0.113.5")
        self.assertEqual(self.resolve("::ffff:203.0.113.5"), "203.0.113.5")
        self.assertEqual(self.resolve("203.0.113.5:4711"), "203.0.113.5")

    def test_the_peer_when_the_entry_is_not_an_address(self):
        for header in ("unknown", "", " ", "203.0.113." + "9" * 5000, "a, b"):
            with self.subTest(header=header[:20]):
                self.assertEqual(self.resolve(header), INGRESS)
        self.assertEqual(self.resolve(), INGRESS)

    def test_an_ipv6_zone_is_not_part_of_the_address(self):
        self.assertEqual(self.resolve("fe80::1%eth0"), "fe80::1")
        self.assertEqual(self.resolve(remote_addr="fe80::1%eth0"), "fe80::1")

    @override_waldur_core_settings(TRUSTED_PROXY_COUNT=2)
    def test_two_proxies(self):
        self.assertEqual(
            self.resolve("198.51.100.1, 203.0.113.5, 10.0.0.2"), "203.0.113.5"
        )
        # Fewer entries than proxies: the peer.
        self.assertEqual(self.resolve("203.0.113.5"), INGRESS)

    @override_waldur_core_settings(TRUSTED_PROXY_COUNT="2")
    def test_a_count_given_as_text(self):
        self.assertEqual(
            self.resolve("198.51.100.1, 203.0.113.5, 10.0.0.2"), "203.0.113.5"
        )

    @override_waldur_core_settings(TRUSTED_PROXY_COUNT=0)
    def test_no_proxy(self):
        self.assertEqual(self.resolve("203.0.113.5"), INGRESS)

    def test_no_address_at_all(self):
        self.assertIsNone(self.resolve("unknown", remote_addr="not-an-address"))
