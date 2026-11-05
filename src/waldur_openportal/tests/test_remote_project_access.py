"""Who may change a remote project (#958).

The award-shaping actions are for staff, global support and the owners of the
organization that owns the remote project's current project. Other users who
see the remote project only read it.
"""

from django.urls import reverse
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import models


class RemoteProjectWriteAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.remote_project = models.RemoteProject.objects.create(
            destination="portal.bridge.offering",
            identifier="proj.portal",
            current_project=self.fixture.project,
        )

    def url(self, action):
        return reverse(
            f"openportal-remote-project-{action}",
            kwargs={"uuid": self.remote_project.uuid.hex},
        )

    def add_note(self, user):
        self.client.force_authenticate(user)
        return self.client.post(self.url("add-note"), {"text": "note"})

    def test_organization_owner_adds_a_note(self):
        response = self.add_note(self.fixture.owner)
        self.assertEqual(response.status_code, 200, response.data)
        self.remote_project.refresh_from_db()
        self.assertEqual(self.remote_project.notes[0]["text"], "note")

    def test_staff_and_global_support_add_a_note(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            self.assertEqual(self.add_note(user).status_code, 200)

    def test_project_members_read_but_do_not_change_it(self):
        for user in (self.fixture.manager, self.fixture.admin, self.fixture.member):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(self.url("detail")).status_code, 200, user)
            response = self.add_note(user)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(
                str(response.data["detail"]), "Organisation owner access required."
            )
        self.remote_project.refresh_from_db()
        self.assertEqual(self.remote_project.notes, [])

    def test_every_write_action_is_refused_to_a_project_member(self):
        self.client.force_authenticate(self.fixture.admin)
        for action in (
            "set-earliest-approve",
            "set-membership-control",
            "set-allowed-domains",
            "set-links",
            "approve-now",
            "hold-indefinitely",
            "reset-to-pending",
            "resend-request",
        ):
            response = self.client.post(self.url(action), {})
            self.assertEqual(response.status_code, 403, action)

    def test_outsider_does_not_find_it(self):
        response = self.add_note(self.fixture.user)
        self.assertEqual(response.status_code, 404)
