from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole, ProposalRole
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
        "call_organizer_user",
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
        # Let the owner see the call, so the 403 comes from the UPDATE_CALL
        # gate rather than from queryset scoping.
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_CALLS)
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


class ProposalProjectRoleMappingVisibilityTest(test.APITestCase):
    """The collection is not public, and shows only the caller's own calls.

    It used to answer anonymous callers with every row in the table.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.other_fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalProjectRoleMappingFactory.get_list_url()
        self.mapping = models.ProposalProjectRoleMapping.objects.create(
            call=self.fixture.call,
            project_role=ProjectRole.MEMBER,
            proposal_role=ProposalRole.MEMBER,
        )
        self.foreign_mapping = models.ProposalProjectRoleMapping.objects.create(
            call=self.other_fixture.call,
            project_role=ProjectRole.MEMBER,
            proposal_role=ProposalRole.MEMBER,
        )

    def test_anonymous_user_cannot_list_mappings(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unrelated_user_sees_no_mappings(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_call_manager_sees_only_own_call(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [row["uuid"] for row in response.json()]
        self.assertEqual(uuids, [self.mapping.uuid.hex])

    def test_call_organizer_sees_own_call(self):
        self.client.force_authenticate(self.fixture.call_organizer_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [row["uuid"] for row in response.json()]
        self.assertEqual(uuids, [self.mapping.uuid.hex])

    def test_customer_role_without_list_calls_sees_no_mappings(self):
        # Seeing a call through its customer takes LIST_CALLS; its mappings
        # follow the call.
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_customer_role_with_list_calls_sees_own_call(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_CALLS)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [row["uuid"] for row in response.json()]
        self.assertEqual(uuids, [self.mapping.uuid.hex])

    def test_foreign_mapping_is_not_retrievable(self):
        self.client.force_authenticate(self.fixture.call_manager)
        url = factories.ProposalProjectRoleMappingFactory.get_url(self.foreign_mapping)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
