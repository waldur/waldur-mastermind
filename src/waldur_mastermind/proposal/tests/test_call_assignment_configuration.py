"""A call's assignment settings, read and changed from the call itself:
the response window of review assignments, reminders and reassignment."""

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, workflow_versions
from waldur_mastermind.proposal.tests import factories


class CallAssignmentConfigurationTest(test.APITestCase):
    def setUp(self):
        # As permissions.yaml grants it to the call's managers.
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.url = factories.CallFactory.get_protected_url(
            self.call, action="assignment-configuration"
        )

    def test_a_call_without_settings_reads_the_defaults(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["assignment_expiration_days"], 7)
        self.assertEqual(str(response.data["call_uuid"]), self.call.uuid.hex)

    def test_the_call_manager_changes_the_response_window(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(self.url, {"assignment_expiration_days": 14})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = models.CallAssignmentConfiguration.objects.get(call=self.call)
        self.assertEqual(config.assignment_expiration_days, 14)

    def test_the_call_it_belongs_to_cannot_be_changed(self):
        other = factories.CallFactory()
        self.client.force_authenticate(self.manager)
        self.client.patch(
            self.url,
            {"call": factories.CallFactory.get_protected_url(other)},
        )
        config = models.CallAssignmentConfiguration.objects.get(call=self.call)
        self.assertEqual(config.call, self.call)

    def test_someone_off_the_call_team_is_refused(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.patch(self.url, {"assignment_expiration_days": 1})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_change_is_recorded_as_a_version_of_the_call(self):
        workflow_versions.ensure_baseline(self.call)
        before = len(list(workflow_versions.owner_versions(self.call)))
        self.client.force_authenticate(self.manager)
        response = self.client.patch(self.url, {"assignment_expiration_days": 21})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertGreater(
            len(list(workflow_versions.owner_versions(self.call))), before
        )
