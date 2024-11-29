from ddt import data, ddt
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import UserFactory


@ddt
class TestProjectQuotas(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.project.set_quota_usage("nc_resource_count", 10)

    @data("staff", "global_support", "owner")
    def test_quotas(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.get(
            reverse("project-quotas-list"), {"quota_name": "nc_resource_count"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[-1]["value"], 10)
        self.assertEqual(response.data[-1]["project_name"], self.project.name)

    def test_user_can_not_access_quotas_of_other_project(self):
        user = UserFactory()
        self.client.force_login(user)
        response = self.client.get(
            reverse("project-quotas-list"), {"quota_name": "nc_resource_count"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
