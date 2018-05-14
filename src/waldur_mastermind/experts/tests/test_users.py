from rest_framework import test
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories

from . import factories


class UserTest(test.APITransactionTestCase):
    def setUp(self):
        self.expert_user = structure_factories.UserFactory()
        self.request_user = structure_factories.UserFactory()
        self.expert_permission = structure_factories.ProjectPermissionFactory(user=self.expert_user)
        request_permission = structure_factories.ProjectPermissionFactory(user=self.request_user)
        self.request = factories.ExpertRequestFactory(project=request_permission.project, user=self.request_user)
        self.other_user = structure_factories.UserFactory()

        self.url = structure_factories.UserFactory.get_list_url()

    def test_if_bid_exists_request_user_can_view_expert_user(self):
        self.client.force_authenticate(user=self.request_user)
        self.contract = factories.ExpertBidFactory(team=self.expert_permission.project, request=self.request)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_if_bid_exists_expert_user_can_view_request_user(self):
        self.client.force_authenticate(user=self.expert_user)
        self.contract = factories.ExpertBidFactory(team=self.expert_permission.project, request=self.request)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_if_bid_dont_exists_request_user_can_view_expert_user(self):
        self.client.force_authenticate(user=self.request_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_if_expert_request_dont_exists_expert_user_cannot_view_request_user(self):
        self.client.force_authenticate(user=self.expert_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_if_bid_exists_other_user_cannot_view_expert_and_request_users(self):
        self.client.force_authenticate(user=self.other_user)
        self.contract = factories.ExpertBidFactory(team=self.expert_permission.project, request=self.request)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
