"""An email its user set does not answer who may sign in with that address.

Callers such as Keycloak ask access_for_email whether an identity with an
address may sign in, and as which account. Holding a user's API token is
enough to set their email, so such an address answers only for the user.
"""

from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

URL = "/api/openportal/access_for_email/"
ADDRESS = "elsewhere@example.com"


class AccessForEmailTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.user = structure_factories.UserFactory(email=ADDRESS)
        self.fixture.project.add_user(self.user, ProjectRole.ADMIN)

    def ask(self, requester):
        self.client.force_authenticate(requester)
        response = self.client.get(URL, {"q": ADDRESS})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_an_email_nobody_changed_answers(self):
        self.assertEqual(self.ask(self.fixture.staff)["status"], "active")

    def test_an_email_the_user_set_does_not_answer_others(self):
        self.user.mark_email_set_by_user()
        self.user.save()

        answer = self.ask(self.fixture.staff)

        self.assertEqual(answer["status"], "unknown")
        self.assertEqual(answer["projects"], {})

    def test_an_email_the_user_set_still_answers_the_user(self):
        self.user.mark_email_set_by_user()
        self.user.save()

        self.assertEqual(self.ask(self.user)["status"], "active")
