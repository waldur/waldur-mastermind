from datetime import timedelta
from unittest import mock

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test
from rest_framework.throttling import ScopedRateThrottle

from waldur_auth_valimo import client, models, tasks, views

AP_PASSWORD = "application-provider-password"


@mock.patch("waldur_auth_valimo.client.requests.post")
class ValimoRequestTest(SimpleTestCase):
    def setUp(self):
        valimo_settings = mock.patch.dict(
            client.Request.settings,
            {
                "URL": "https://mssp.example.com",
                "AP_ID": "http://ap.example.com",
                "AP_PWD": AP_PASSWORD,
                "SignatureProfile": "http://mss.example.com/profile",
            },
        )
        valimo_settings.start()
        self.addCleanup(valimo_settings.stop)

    def execute(self, post):
        post.return_value = mock.Mock(ok=False, status_code=500, content=b"")
        with self.assertRaises(client.RequestError):
            client.SignatureRequest.execute(
                transaction_id="transaction", phone="+3725555555", message="1234"
            )
        return post.call_args.kwargs

    def test_tls_certificate_of_the_service_is_verified_by_default(self, post):
        self.assertIs(self.execute(post)["verify"], True)

    def test_request_has_a_timeout(self, post):
        self.assertTrue(self.execute(post)["timeout"])

    def test_error_message_carries_no_response_content(self, post):
        post.return_value = mock.Mock(
            ok=False, status_code=500, content=b"<Fault>secret detail</Fault>"
        )
        with self.assertRaises(client.RequestError) as context:
            client.SignatureRequest.execute(
                transaction_id="transaction", phone="+3725555555", message="1234"
            )
        self.assertNotIn("secret detail", str(context.exception))

    def test_unreachable_service_raises_service_unavailable(self, post):
        post.side_effect = requests.ConnectTimeout("mssp.example.com timed out")
        with self.assertRaises(client.ServiceUnavailableError) as context:
            client.SignatureRequest.execute(
                transaction_id="transaction", phone="+3725555555", message="1234"
            )
        self.assertNotIn("mssp.example.com", str(context.exception))

    def test_application_provider_password_is_not_logged(self, post):
        with self.assertLogs("waldur_auth_valimo.client", level="DEBUG") as logs:
            self.execute(post)

        self.assertNotIn(AP_PASSWORD, "\n".join(logs.output))


class ValimoThrottleTest(SimpleTestCase):
    def test_starting_a_login_is_rate_limited(self):
        view = views.AuthResultViewSet(action="create")
        self.assertIn(
            view.throttle_scope, settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        )

    def test_polling_the_login_result_is_not_rate_limited(self):
        # The portal polls the result until the user confirms on the phone.
        view = views.AuthResultViewSet(action="result")
        self.assertIsNone(view.throttle_scope)


@mock.patch("waldur_auth_valimo.tasks.client.StatusRequest.execute")
class ValimoPollTest(TestCase):
    def test_unreachable_service_is_polled_again(self, execute):
        auth_result = models.AuthResult.objects.create(
            phone="+3725555555", state=models.AuthResult.States.PROCESSING
        )
        execute.side_effect = client.ServiceUnavailableError("unavailable")
        with mock.patch.object(tasks.PollTask, "retry") as retry:
            tasks.PollTask().execute(auth_result)
        retry.assert_called_once()


@mock.patch("waldur_auth_valimo.executors.AuthExecutor.execute")
class ValimoLoginStartTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        settings_patch = mock.patch.dict(
            settings.WALDUR_CORE, {"AUTHENTICATION_METHODS": ["VALIMO"]}
        )
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.url = reverse("auth-valimo-list")

    def start(self, phone="+3725555555"):
        return self.client.post(self.url, {"phone": phone})

    def test_login_is_refused_while_another_one_for_the_phone_is_in_progress(
        self, execute
    ):
        self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.start().status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def make_logins(self, count, state=models.AuthResult.States.CANCELED):
        for _ in range(count):
            models.AuthResult.objects.create(phone="+3725555555", state=state)

    def test_unsuccessful_logins_for_a_phone_are_limited_per_hour(self, execute):
        self.make_logins(10)
        response = self.start()
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("Retry-After", response)

    def test_successful_logins_do_not_count(self, execute):
        self.make_logins(20, state=models.AuthResult.States.OK)
        self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)

    def test_hourly_limit_can_be_switched_off(self, execute):
        self.make_logins(20)
        with mock.patch.dict(
            settings.WALDUR_AUTH_VALIMO, {"LOGIN_ATTEMPTS_PER_PHONE_PER_HOUR": 0}
        ):
            self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)

    def test_phone_with_international_prefix_shares_the_limit(self, execute):
        self.assertEqual(self.start("+3725555555").status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.start("003725555555").status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    def test_formatting_the_phone_differently_does_not_avoid_the_limit(self, execute):
        self.assertEqual(
            self.start("+372 5555 555").status_code, status.HTTP_201_CREATED
        )
        self.assertEqual(
            self.start("+372-5555-555").status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    def test_old_logins_do_not_count(self, execute):
        old = models.AuthResult.objects.create(
            phone="+3725555555", state=models.AuthResult.States.PROCESSING
        )
        models.AuthResult.objects.filter(pk=old.pk).update(
            created=timezone.now() - timedelta(hours=2)
        )
        self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)


@mock.patch("waldur_auth_valimo.executors.AuthExecutor.execute")
class ValimoThrottleApiTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        for patch in (
            mock.patch.object(
                views.AuthResultViewSet, "throttle_classes", [ScopedRateThrottle]
            ),
            mock.patch.object(
                ScopedRateThrottle, "THROTTLE_RATES", {"valimo": "2/min"}
            ),
            mock.patch.dict(
                settings.WALDUR_CORE, {"AUTHENTICATION_METHODS": ["VALIMO"]}
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_starting_logins_is_throttled_per_client_but_polling_is_not(self, execute):
        url = reverse("auth-valimo-list")
        uuids = [
            self.client.post(url, {"phone": f"+37255555{n}"}).data["uuid"]
            for n in range(2)
        ]
        response = self.client.post(url, {"phone": "+372555559"})
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        for _ in range(5):
            response = self.client.post(
                reverse("auth-valimo-result"), {"uuid": uuids[0]}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
