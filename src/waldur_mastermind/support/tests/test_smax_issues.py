from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import factories, smax_base
from waldur_smax.backend import Issue


class IssueCreateTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.url = factories.IssueFactory.get_list_url()
        self.caller = structure_factories.UserFactory()
        self.smax_issue = Issue(1, 'test', 'description')
        self.mock_smax().add_issue.return_value = self.smax_issue

    def _get_valid_payload(self, **additional):
        payload = {
            'summary': 'test_issue',
            'caller': structure_factories.UserFactory.get_url(user=self.caller),
        }
        payload.update(additional)
        return payload

    def test_create_issue(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.mock_smax().add_issue.assert_called_once()
        issue = models.Issue.objects.get(uuid=response.data['uuid'])
        self.assertEqual(str(issue.backend_id), str(self.smax_issue.id))
