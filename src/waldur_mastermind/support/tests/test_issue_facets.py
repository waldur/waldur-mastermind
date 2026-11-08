from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support.tests import factories


def values(response, name):
    [facet] = [f for f in response.data if f["name"] == name]
    return {v["value"]: v["count"] for v in facet["values"]}


class IssueFacetsTest(test.APITestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        # Status and type are free text synced from the service desk.
        factories.IssueFactory(
            status="Waiting for customer", type="Incident", project=self.project
        )
        factories.IssueFactory(status="Waiting for customer", type="Service Request")
        factories.IssueFactory(status="Resolved", type="Incident")
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def get(self, **params):
        response = self.client.get(reverse("support-issue-facets"), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def test_statuses_come_from_the_issues(self):
        response = self.get()

        self.assertEqual(
            values(response, "status"), {"Waiting for customer": 2, "Resolved": 1}
        )
        self.assertEqual(
            values(response, "type"), {"Incident": 2, "Service Request": 1}
        )

    def test_other_filters_narrow_and_own_filter_is_ignored(self):
        response = self.get(status="Resolved", type="Incident")

        self.assertEqual(
            values(response, "status"), {"Waiting for customer": 1, "Resolved": 1}
        )
        self.assertEqual(values(response, "type"), {"Incident": 1})

    def test_project_facet(self):
        response = self.get(
            facet="project", type="Incident", status="Waiting for customer"
        )
        self.assertEqual(values(response, "project"), {self.project.uuid.hex: 1})
