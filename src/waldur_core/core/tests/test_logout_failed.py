"""The logout-failed redirect carries a code and nothing else.

The frontend shows its own text for each code, so a crafted link cannot make
the portal show text of someone else's choosing.
"""

from urllib.parse import parse_qs, urlparse

from django.test import TestCase

from waldur_core.core.enums import LogoutFailureCode
from waldur_core.core.views import logout_failed


class LogoutFailedRedirectTest(TestCase):
    def test_every_code_is_the_only_parameter(self):
        for code in LogoutFailureCode:
            with self.subTest(code=code.value):
                location = urlparse(logout_failed(code).url)
                self.assertTrue(location.path.endswith("/logout_failed/"))
                self.assertEqual(parse_qs(location.query), {"code": [code.value]})

    def test_text_is_refused(self):
        with self.assertRaises(ValueError):
            logout_failed("Your session expired, sign in at https://attacker.example")
