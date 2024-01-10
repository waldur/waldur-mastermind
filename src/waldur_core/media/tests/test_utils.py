from freezegun import freeze_time
from rest_framework import test
from rest_framework.exceptions import ValidationError

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.media.utils import decode_attachment_token, encode_attachment_token
from waldur_core.structure.tests.factories import CustomerFactory, UserFactory


@override_waldur_core_settings(TIME_ZONE="Asia/Muscat")
class TestMediaUtils(test.APITransactionTestCase):
    def setUp(self):
        self.user = UserFactory()
        self.customer = CustomerFactory()

    def test_token_encoder(self):
        token = encode_attachment_token(self.user.uuid.hex, self.customer, "image")
        user_uuid, content_type, object_id, field = decode_attachment_token(token)
        self.assertEqual(self.user.uuid.hex, user_uuid)
        self.assertEqual(field, "image")
        self.assertEqual(object_id, self.customer.uuid.hex)

    def test_expired_token(self):
        with freeze_time("2019-01-01"):
            token = encode_attachment_token(self.user.uuid.hex, self.customer, "image")
        with freeze_time("2019-01-02"):
            self.assertRaises(ValidationError, decode_attachment_token, token)
