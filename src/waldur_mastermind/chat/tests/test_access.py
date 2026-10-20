from unittest import mock

from django.test import TestCase

from waldur_core.configuration.testing import override_config
from waldur_mastermind.chat import access


def _user(is_staff=False, is_support=False):
    return mock.Mock(is_staff=is_staff, is_support=is_support)


class RoleRefusalTest(TestCase):
    STAFF = _user(is_staff=True)
    SUPPORT = _user(is_support=True)
    USER = _user()

    def _allowed(self, roles):
        with override_config(AI_ASSISTANT_ENABLED_ROLES=roles):
            return [
                access.role_refusal(user) is None
                for user in (self.STAFF, self.SUPPORT, self.USER)
            ]

    def test_all_and_anonymous_let_everyone_in(self):
        self.assertEqual(self._allowed("all"), [True, True, True])
        self.assertEqual(self._allowed("anonymous"), [True, True, True])

    def test_staff_lets_only_staff_in(self):
        self.assertEqual(self._allowed("staff"), [True, False, False])

    def test_staff_and_support_lets_both_in(self):
        self.assertEqual(self._allowed("staff_and_support"), [True, True, False])

    def test_disabled_lets_nobody_in(self):
        self.assertEqual(self._allowed("disabled"), [False, False, False])

    def test_an_unknown_value_lets_nobody_in(self):
        self.assertEqual(self._allowed("everyone"), [False, False, False])

    @override_config(AI_ASSISTANT_ENABLED_ROLES="staff")
    def test_the_refusal_says_who_may_use_it(self):
        self.assertEqual(
            access.role_refusal(self.USER),
            "AI Assistant is currently available to staff users only.",
        )
