from unittest.mock import patch

from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories

UPDATE_ACTIONS_URL = "/api/user-actions/update_actions/"


@patch("waldur_core.user_actions.tasks.update_user_actions.delay")
class UpdateActionsTest(APITestCase):
    """Recalculating every user's actions is for staff only."""

    def test_staff_schedules_recalculation(self, delay):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

        response = self.client.post(UPDATE_ACTIONS_URL, {})

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        delay.assert_called_once_with(provider_action_type=None)

    def test_support_may_not_schedule_recalculation(self, delay):
        self.client.force_authenticate(structure_factories.UserFactory(is_support=True))

        response = self.client.post(UPDATE_ACTIONS_URL, {})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        delay.assert_not_called()

    def test_user_may_not_schedule_recalculation(self, delay):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.post(
            UPDATE_ACTIONS_URL, {"provider_action_type": "pending_order"}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        delay.assert_not_called()

    def test_anonymous_may_not_schedule_recalculation(self, delay):
        response = self.client.post(UPDATE_ACTIONS_URL, {})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        delay.assert_not_called()
