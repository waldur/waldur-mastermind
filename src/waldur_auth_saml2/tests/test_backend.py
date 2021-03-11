from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from waldur_auth_saml2.auth import WaldurSaml2Backend

User = get_user_model()


class WaldurSaml2BackendTest(TestCase):
    def test_email_should_be_unique_positive(self):
        User.objects.create(username='john', email='john@example.com')
        attribute_mapping = {
            'uid': ('username',),
            'mail': ('email',),
        }
        attributes = {
            'uid': ['john'],
            'mail': ['john@example.com'],
        }
        session_info = {'ava': attributes, 'issuer': 'IDP'}

        backend = WaldurSaml2Backend()
        user = backend.authenticate(
            None, session_info=session_info, attribute_mapping=attribute_mapping,
        )
        self.assertIsNotNone(user)

    def test_email_should_be_unique_negative(self):
        User.objects.create(username='harry', email='john@example.com')
        attribute_mapping = {
            'uid': ('username',),
            'mail': ('email',),
        }
        attributes = {
            'uid': ['john'],
            'mail': ['john@example.com'],
        }
        session_info = {'ava': attributes, 'issuer': 'IDP'}

        backend = WaldurSaml2Backend()
        self.assertRaises(
            ValidationError,
            backend.authenticate,
            None,
            session_info=session_info,
            attribute_mapping=attribute_mapping,
        )
