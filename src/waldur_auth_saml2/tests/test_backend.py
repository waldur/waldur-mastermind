from django.contrib.auth import get_user_model
from django.test import TestCase
from djangosaml2.backends import Saml2Backend

User = get_user_model()


class Saml2BackendTest(TestCase):
    def test_email_may_be_duplicate(self):
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

        backend = Saml2Backend()
        user = backend.authenticate(
            None,
            session_info=session_info,
            attribute_mapping=attribute_mapping,
        )
        self.assertIsNotNone(user)
