from django.test import TestCase
from rest_framework.authtoken.models import Token

from waldur_core.core.auth_utils import get_auth_method, is_pat_auth
from waldur_core.core.models import PersonalAccessToken


class GetAuthMethodTest(TestCase):
    def test_none_is_session(self):
        self.assertEqual(get_auth_method(None), "session")

    def test_personal_access_token(self):
        self.assertEqual(get_auth_method(PersonalAccessToken()), "pat")

    def test_drf_token(self):
        self.assertEqual(get_auth_method(Token()), "token")

    def test_jwt_payload_dict(self):
        self.assertEqual(get_auth_method({"sub": "abc"}), "oidc")

    def test_unrecognised_object(self):
        self.assertEqual(get_auth_method(object()), "unknown")


class IsPatAuthTest(TestCase):
    def test_true_for_pat(self):
        self.assertTrue(is_pat_auth(PersonalAccessToken()))

    def test_false_for_session(self):
        self.assertFalse(is_pat_auth(None))
