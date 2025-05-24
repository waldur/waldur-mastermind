from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from waldur_core.core.models import User


class TestModels(TestCase):
    def test_token_lifetime_is_read_from_settings_as_default_value_when_user_is_created(
        self,
    ):
        waldur_core_settings = settings.WALDUR_CORE.copy()
        waldur_core_settings["TOKEN_LIFETIME"] = timezone.timedelta(days=1)

        with self.settings(WALDUR_CORE=waldur_core_settings):
            token_lifetime = settings.WALDUR_CORE["TOKEN_LIFETIME"]
            expected_lifetime = int(token_lifetime.total_seconds())
            user = User.objects.create(username="test1")
            self.assertEqual(user.token_lifetime, expected_lifetime)

    def test_when_user_is_created_query_field_is_filled(self):
        user = User.objects.create_user(username="jb007", full_name="J̋̀a̻͢m̪̄e̪͊s̯̊ B̝͆on͎̂d")
        self.assertEqual(user.query_field, "James Bond")
