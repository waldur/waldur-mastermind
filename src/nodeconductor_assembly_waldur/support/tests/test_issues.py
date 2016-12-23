import json

from ddt import ddt, data
from rest_framework import status

from nodeconductor.structure.tests import factories as structure_factories

from . import factories, base
from .. import models


@ddt
class IssueRetreiveTest(base.BaseTest):

    @data('staff', 'owner')
    def test_user_can_access_customer_issue_if_he_has_customer_level_permission(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer)

        response = self.client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('admin', 'manager', 'user')
    def test_user_cannot_access_customer_issue_if_he_has_no_permission(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer)

        response = self.client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_access_project_issue_if_he_has_project_level_permission(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer, project=self.fixture.project)

        response = self.client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_user_cannot_access_project_issue_if_he_has_no_project_level_permission(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer, project=self.fixture.project)

        response = self.client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class IssueCreateTest(base.BaseTest):

    def setUp(self):
        super(IssueCreateTest, self).setUp()
        self.url = factories.IssueFactory.get_list_url()

    def test_staff_can_create_issue_if_he_has_support_user(self):
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_cannot_create_issue_if_he_does_not_have_support_user(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('staff', 'owner')
    def test_user_with_access_to_customer_can_create_customer_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            customer=structure_factories.CustomerFactory.get_url(self.fixture.customer),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Issue.objects.filter(summary=payload['summary']).exists())

    @data('admin', 'manager', 'user')
    def test_user_without_access_to_customer_cannot_create_customer_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            customer=structure_factories.CustomerFactory.get_url(self.fixture.customer),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Issue.objects.filter(summary=payload['summary']).exists())

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_with_access_to_project_can_create_project_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            project=structure_factories.ProjectFactory.get_url(self.fixture.project),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Issue.objects.filter(summary=payload['summary']).exists())

    @data('user')
    def test_user_without_access_to_project_cannot_create_project_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            project=structure_factories.ProjectFactory.get_url(self.fixture.project),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Issue.objects.filter(summary=payload['summary']).exists())

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_with_access_to_resource_can_create_resource_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            resource=structure_factories.TestInstanceFactory.get_url(self.fixture.resource),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Issue.objects.filter(summary=payload['summary']).exists())

    @data('user')
    def test_user_without_access_to_resource_cannot_create_resource_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            resource=structure_factories.TestInstanceFactory.get_url(self.fixture.resource),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Issue.objects.filter(summary=payload['summary']).exists())

    def test_project_issue_populates_customer_field_on_creation(self):
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload(
            project=structure_factories.ProjectFactory.get_url(self.fixture.project))

        response = self.client.post(self.url, data=payload)

        issue = models.Issue.objects.get(uuid=json.loads(response.content)['uuid'])
        self.assertEqual(issue.customer, self.fixture.project.customer)

    def test_resource_issue_populated_customer_and_project_field_on_creation(self):
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload(
            resource=structure_factories.TestInstanceFactory.get_url(self.fixture.resource))

        response = self.client.post(self.url, data=payload)

        issue = models.Issue.objects.get(uuid=json.loads(response.content)['uuid'])
        self.assertEqual(issue.project, self.fixture.resource.service_project_link.project)
        self.assertEqual(issue.customer, self.fixture.resource.service_project_link.project.customer)

    def _get_valid_payload(self, **additional):
        payload = {
            'summary': 'test_issue',
            'caller': structure_factories.UserFactory.get_url(),
        }
        payload.update(additional)
        return payload


@ddt
class IssueUpdateTest(base.BaseTest):

    def setUp(self):
        super(IssueUpdateTest, self).setUp()
        self.issue = factories.IssueFactory(customer=self.fixture.customer, project=self.fixture.project)
        self.url = factories.IssueFactory.get_url(self.issue)

    def test_staff_can_edit_issue(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(models.Issue.objects.filter(summary=payload['summary']).exists())

    @data('owner', 'admin', 'manager')
    def test_nonstaff_user_cannot_edit_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Issue.objects.filter(summary=payload['summary']).exists())

    def _get_valid_payload(self):
        return {'summary': 'edited_summary'}


@ddt
class IssueDeleteTest(base.BaseTest):

    def setUp(self):
        super(IssueDeleteTest, self).setUp()
        self.issue = factories.IssueFactory(customer=self.fixture.customer, project=self.fixture.project)
        self.url = factories.IssueFactory.get_url(self.issue)

    def test_staff_can_delete_issue(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Issue.objects.filter(id=self.issue.id).exists())

    @data('owner', 'admin', 'manager')
    def test_nonstaff_user_cannot_delete_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Issue.objects.filter(id=self.issue.id).exists())


@ddt
class IssueCommentTest(base.BaseTest):

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_with_access_to_issue_can_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer, project=self.fixture.project)
        payload = self._get_valid_payload()

        response = self.client.post(factories.IssueFactory.get_url(issue, action='comment'), data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Comment.objects.filter(issue=issue, description=payload['description']))

    @data('admin', 'manager', 'user')
    def test_user_without_access_to_instance_cannot_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer)
        payload = self._get_valid_payload()

        response = self.client.post(factories.IssueFactory.get_url(issue, action='comment'), data=payload)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(models.Comment.objects.filter(issue=issue, description=payload['description']))

    def _get_valid_payload(self):
        return {'description': 'Comment description'}
