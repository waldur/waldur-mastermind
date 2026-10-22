"""Estimated-cost policies keep a version history (#602).

The `policy_created` / `policy_updated` events say that a policy changed; the
history keeps every state it had, and who saved it, so a limit, period, credit
setting or action list can be traced back to the version that introduced it.
"""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories as policy_factories


class ProjectCostPolicyHistoryTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project

    def _create(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            policy_factories.ProjectEstimatedCostPolicyFactory.get_list_url(),
            {
                "scope": structure_factories.ProjectFactory.get_url(self.project),
                "limit_cost": 100,
                "period": ProjectEstimatedCostPolicy.Periods.TOTAL,
                "actions": "notify_project_team",
                "use_credit": True,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data["url"]

    def _history(self, url, user):
        self.client.force_authenticate(user)
        return self.client.get(url + "history/")

    def test_create_and_update_are_versioned_with_the_user(self):
        url = self._create()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(url, {"limit_cost": 250})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        response = self._history(url, self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Newest first.
        updated, created = response.data
        self.assertEqual(updated["serialized_data"]["limit_cost"], 250)
        self.assertEqual(created["serialized_data"]["limit_cost"], 100)
        self.assertEqual(updated["revision_user"]["uuid"], str(self.fixture.owner.uuid))
        self.assertEqual(created["revision_user"]["uuid"], str(self.fixture.staff.uuid))

    def test_evaluation_saves_add_no_version(self):
        # A policy save that only records an evaluation (has_fired) happens
        # outside any revision, so the history stays the user's changes.
        url = self._create()
        policy = ProjectEstimatedCostPolicy.objects.get()
        policy.has_fired = not policy.has_fired
        policy.save(update_fields=["has_fired"])

        response = self._history(url, self.fixture.staff)

        self.assertEqual(len(response.data), 1)

    def test_history_is_for_staff_and_support_only(self):
        url = self._create()

        response = self._history(url, self.fixture.owner)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
