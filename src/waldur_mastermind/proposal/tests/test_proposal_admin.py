"""The proposal administrator: edits a proposal, but neither submits it nor
manages its team."""

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, utils
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures

ADMIN = RoleEnum.PROPOSAL_ADMIN


class ProposalAdminMixin:
    def setUp(self):
        super().setUp()
        # Mirrors permissions.yaml.
        ProposalRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL)
        ProposalRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROPOSAL)
        ProposalRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROPOSAL_PERMISSION)
        ProposalRole.MANAGER.add_permission(PermissionEnum.DELETE_PROPOSAL_PERMISSION)
        ProposalRole.ADMIN.add_permission(PermissionEnum.UPDATE_PROPOSAL)
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.applicant = self.proposal.created_by
        self.proposal.add_user(self.applicant, ProposalRole.MANAGER)
        self.admin = structure_factories.UserFactory()
        self.proposal.add_user(self.admin, ProposalRole.ADMIN)
        self.member = structure_factories.UserFactory()
        self.proposal.add_user(self.member, ProposalRole.MEMBER)
        self.co_manager = structure_factories.UserFactory()
        self.proposal.add_user(self.co_manager, ProposalRole.MANAGER)

    def _url(self, action=None):
        return factories.ProposalFactory.get_url(self.proposal, action=action)


class ProposalAdminEditsTest(ProposalAdminMixin, test.APITestCase):
    def _update_details(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            self._url("update_project_details"), {"name": "Renamed"}
        )

    def _add_resource(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            self._url("resources"),
            {
                "requested_offering_uuid": (
                    self.fixture.requested_offering_accepted.uuid.hex
                )
            },
        )

    def _resource_url(self):
        return factories.RequestedResourceFactory.get_url(
            self.proposal, self.fixture.requested_resource
        )

    def _attach(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            self._url("attach_document"),
            {"file": SimpleUploadedFile("proposal.pdf", b"%PDF-1.4\n%%EOF\n")},
            format="multipart",
        )

    def test_admin_updates_project_details(self):
        response = self._update_details(self.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.name, "Renamed")

    def test_co_manager_updates_project_details(self):
        response = self._update_details(self.co_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_member_cannot_update_project_details(self):
        response = self._update_details(self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_manages_requested_resources(self):
        response = self._add_resource(self.admin)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        url = self._resource_url()
        self.client.force_authenticate(self.admin)
        response = self.client.patch(url, {"description": "changed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_member_cannot_change_requested_resources(self):
        self.assertEqual(
            self._add_resource(self.member).status_code, status.HTTP_403_FORBIDDEN
        )
        url = self._resource_url()
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.patch(url, {"description": "changed"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_member_still_reads_requested_resources(self):
        self.fixture.requested_resource
        self.client.force_authenticate(self.member)
        response = self.client.get(self._url("resources"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_reviewer_cannot_add_requested_resources(self):
        response = self._add_resource(self.fixture.reviewer_1)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_admin_attaches_and_detaches_documents(self):
        response = self._attach(self.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        document = self.proposal.proposaldocumentation_set.get()
        response = self.client.post(
            self._url("detach_documents"), {"documents": [document.uuid.hex]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(self.proposal.proposaldocumentation_set.exists())

    def test_member_cannot_attach_or_detach_documents(self):
        self.assertEqual(self._attach(self.member).status_code, 403)
        document = models.ProposalDocumentation.objects.create(
            proposal=self.proposal,
            file=SimpleUploadedFile("proposal.pdf", b"%PDF-1.4\n%%EOF\n"),
        )
        self.client.force_authenticate(self.member)
        response = self.client.post(
            self._url("detach_documents"), {"documents": [document.uuid.hex]}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(self.proposal.proposaldocumentation_set.exists())

    def test_admin_reads_the_compliance_checklist_status(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self._url("completion_status"))
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_cannot_submit_checklist_answers(self):
        self.client.force_authenticate(self.member)
        response = self.client.post(self._url("submit_answers"), [], format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProposalAdminLimitsTest(ProposalAdminMixin, test.APITestCase):
    def test_admin_cannot_submit(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self._url("submit"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

    def test_co_manager_may_submit(self):
        self.client.force_authenticate(self.co_manager)
        response = self.client.post(self._url("submit"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_admin_cannot_delete_the_proposal(self):
        self.client.force_authenticate(self.admin)
        response = self.client.delete(self._url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Proposal.objects.filter(pk=self.proposal.pk).exists())

    def test_admin_cannot_manage_the_team(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self._url("add_user"),
            {
                "user": structure_factories.UserFactory().uuid.hex,
                "role": RoleEnum.PROPOSAL_MEMBER,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.post(
            self._url("delete_user"),
            {"user": self.member.uuid.hex, "role": RoleEnum.PROPOSAL_MEMBER},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_does_not_count_as_a_manager_for_submission(self):
        self.proposal.remove_user(self.applicant, ProposalRole.MANAGER)
        self.proposal.remove_user(self.co_manager, ProposalRole.MANAGER)
        self.assertFalse(self.proposal.can_submit()[0])


class ProposalAdminGrantTest(ProposalAdminMixin, test.APITestCase):
    def _grant(self, user, as_user):
        self.client.force_authenticate(as_user)
        return self.client.post(
            self._url("add_user"), {"user": user.uuid.hex, "role": ADMIN}
        )

    def _revoke(self, user, as_user):
        self.client.force_authenticate(as_user)
        return self.client.post(
            self._url("delete_user"), {"user": user.uuid.hex, "role": ADMIN}
        )

    def test_manager_grants_and_revokes_admin(self):
        outsider = structure_factories.UserFactory()
        self.assertEqual(
            self._grant(outsider, self.applicant).status_code, status.HTTP_201_CREATED
        )
        self.assertEqual(
            self._revoke(outsider, self.applicant).status_code, status.HTTP_200_OK
        )

    def test_call_manager_grants_and_revokes_admin_on_a_draft(self):
        call_manager = self.fixture.call_manager
        self.assertEqual(
            self._grant(self.member, call_manager).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self._revoke(self.member, call_manager).status_code, status.HTTP_200_OK
        )

    def test_admin_cannot_grant_admin(self):
        response = self._grant(self.member, self.admin)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_role_is_frozen_after_submission(self):
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()
        self.assertEqual(
            self._grant(self.member, self.applicant).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self._revoke(self.admin, self.applicant).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self._revoke(self.admin, self.fixture.call_manager).status_code,
            status.HTTP_200_OK,
        )


class DefaultRoleMappingTest(test.APITestCase):
    def _mappings(self, call):
        return {
            (m.proposal_role.name, m.project_role.name if m.project_role else None)
            for m in call.proposalprojectrolemapping_set.all()
        }

    def test_new_call_maps_proposal_roles_to_project_roles(self):
        call = fixtures.ProposalFixture().call
        self.assertEqual(
            self._mappings(call),
            {
                (RoleEnum.PROPOSAL_MANAGER, RoleEnum.PROJECT_MANAGER),
                (RoleEnum.PROPOSAL_ADMIN, RoleEnum.PROJECT_ADMIN),
                (RoleEnum.PROPOSAL_MEMBER, RoleEnum.PROJECT_MEMBER),
            },
        )

    def test_saving_an_existing_call_leaves_its_mappings_alone(self):
        call = fixtures.ProposalFixture().call
        call.proposalprojectrolemapping_set.all().delete()
        call.name = "Renamed"
        call.save()
        self.assertEqual(self._mappings(call), set())

    def test_duplicate_copies_the_source_mappings(self):
        fixture = fixtures.ProposalFixture()
        call = fixture.call
        call.proposalprojectrolemapping_set.exclude(
            proposal_role__name=RoleEnum.PROPOSAL_MANAGER
        ).delete()
        call.proposalprojectrolemapping_set.update(project_role=ProjectRole.ADMIN)

        duplicate = utils.duplicate_call(call, "Copy", fixture.staff)
        self.assertEqual(
            self._mappings(duplicate),
            {(RoleEnum.PROPOSAL_MANAGER, RoleEnum.PROJECT_ADMIN)},
        )
