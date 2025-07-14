from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ProposalProjectRoleMappingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.ProposalProjectRoleMappingFactory.get_list_url()
        self.call_protected_url = factories.CallFactory.get_protected_url(self.call)

    def _auth_and_create_mapping(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url,
            {
                "call": self.call_protected_url,
                "project_role": ProjectRole.MEMBER.name,
                "proposal_role": ProposalRole.MEMBER.name,
            },
        )
        return response

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_create_update_delete_mapping(self, user):
        response = self._auth_and_create_mapping(user)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["project_role"], ProjectRole.MEMBER.name)
        self.assertEqual(response.data["proposal_role"], ProposalRole.MEMBER.name)

        update_payload = {
            "project_role": ProjectRole.ADMIN.name,
        }
        response = self.client.put(
            response.data["url"],
            update_payload,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["project_role"], ProjectRole.ADMIN.name)
        self.assertEqual(response.data["proposal_role"], ProposalRole.MEMBER.name)

        response = self.client.delete(response.data["url"])
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "owner",
        "proposal_creator",
        "reviewer_1",
        "global_support",
        "user",
    )
    def test_user_cannot_create_mapping(self, user):
        response = self._auth_and_create_mapping(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        "owner",
    )
    def test_user_cannot_update_delete_mapping(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        mapping = models.ProposalProjectRoleMapping.objects.create(
            call=self.call,
            project_role=ProjectRole.MEMBER,
            proposal_role=ProposalRole.MEMBER,
        )
        url = factories.ProposalProjectRoleMappingFactory.get_url(mapping)
        response = self.client.put(
            url,
            {
                "project_role": ProjectRole.ADMIN.name,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
