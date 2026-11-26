"""The login-failed redirect carries a code, and the page's texts are public.

The frontend shows its own text for each code, and the configured text for the
two OIDC block codes, which it reads from the public configuration. Nothing
else travels in the URL, so a crafted link cannot make the portal show text of
someone else's choosing.
"""

from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.test import TestCase
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import LoginFailureCode
from waldur_core.core.views import login_failed


class LoginFailedRedirectTest(TestCase):
    def test_every_code_is_the_only_parameter(self):
        for code in LoginFailureCode:
            with self.subTest(code=code.value):
                location = urlparse(login_failed(code).url)
                self.assertTrue(location.path.endswith("/login_failed/"))
                self.assertEqual(parse_qs(location.query), {"code": [code.value]})

    def test_text_is_refused(self):
        with self.assertRaises(ValueError):
            login_failed("Your account is locked, visit https://attacker.example")


class LoginFailedMessagesArePublicTest(test.APITestCase):
    def setUp(self):
        cache.delete("API_CONFIGURATION")

    def tearDown(self):
        cache.delete("API_CONFIGURATION")

    @override_config(
        OIDC_BLOCKED_LOGIN_RESPONSE_MESSAGE="Ask your administrator for access.",
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE="Signup is closed.",
    )
    def test_anonymous_configuration_carries_the_block_messages(self):
        response = self.client.get("/api/configuration/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        core = response.data["WALDUR_CORE"]
        self.assertEqual(
            core["OIDC_BLOCKED_LOGIN_RESPONSE_MESSAGE"],
            "Ask your administrator for access.",
        )
        self.assertEqual(
            core["OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE"],
            "Signup is closed.",
        )
