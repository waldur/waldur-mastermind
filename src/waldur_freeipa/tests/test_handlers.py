from unittest import mock

from django.test import TestCase

from waldur_core.core import utils as core_utils
from waldur_freeipa.tests import factories


class UserStatusSyncTest(TestCase):
    def setUp(self):
        self.profile = factories.ProfileFactory(is_active=True)
        self.user = self.profile.user

    @mock.patch("waldur_freeipa.handlers.tasks.user_enable.delay")
    @mock.patch("waldur_freeipa.handlers.tasks.user_disable.delay")
    def test_user_state_sync(self, mock_user_disable, mock_user_enable):
        # Activate the user
        self.user.is_active = False
        self.user.save()

        # Check that the user_disable task is called
        mock_user_disable.assert_called_once_with(
            core_utils.serialize_instance(self.profile)
        )
        mock_user_enable.assert_not_called()

        mock_user_disable.reset_mock()
        mock_user_enable.reset_mock()

        # Deactivate the user
        self.user.is_active = True
        self.user.save()

        # Check that the user_disable task is called
        mock_user_disable.assert_not_called()
        mock_user_enable.assert_called_once_with(
            core_utils.serialize_instance(self.profile)
        )
