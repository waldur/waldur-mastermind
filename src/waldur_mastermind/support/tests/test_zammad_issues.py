from django.conf import settings
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import factories, zammad_base
from waldur_zammad.backend import Issue


class IssueCreateTest(zammad_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.url = factories.IssueFactory.get_list_url()
        self.caller = structure_factories.UserFactory()
        factories.SupportCustomerFactory(user=self.caller)

        self.zammad_issue = Issue(1, 'open', 'test_issue')
        self.mock_zammad().add_issue.return_value = self.zammad_issue

    def _get_valid_payload(self, **additional):
        is_reported_manually = additional.get('is_reported_manually')
        issue_type = settings.WALDUR_SUPPORT['ISSUE']['types'][0]
        factories.RequestTypeFactory(issue_type_name=issue_type)
        payload = {
            'summary': 'test_issue',
            'type': issue_type,
        }

        if is_reported_manually:
            payload['is_reported_manually'] = True
        else:
            payload['caller'] = structure_factories.UserFactory.get_url(
                user=self.caller
            )

        payload.update(additional)
        return payload

    def test_create_issue(self):
        user = self.fixture.staff
        factories.SupportUserFactory(user=user, backend_name='zammad')
        self.client.force_authenticate(user)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.mock_zammad().add_issue.assert_called_once()
        issue = models.Issue.objects.get(uuid=response.data['uuid'])
        self.assertEqual(str(issue.backend_id), str(self.zammad_issue.id))
        self.assertEqual(issue.status, self.zammad_issue.status)


class IssueWebHookTest(zammad_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.issue = factories.IssueFactory(backend_id=1)
        self.url = '/api/support-zammad-webhook/'
        self.zammad_issue = Issue(1, 'open', 'test_issue')
        self.mock_zammad().get_issue.return_value = self.zammad_issue

    def test_update_issue(self):
        response = self.client.post(self.url, data={'ticket': {'id': '1'}})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mock_zammad().get_issue.assert_called_once()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, self.zammad_issue.status)
        self.assertEqual(self.issue.summary, self.zammad_issue.summary)
