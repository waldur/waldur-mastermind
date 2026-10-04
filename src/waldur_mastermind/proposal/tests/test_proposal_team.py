"""The proposal team: who manages it, and when it may change.

The principal investigator of a proposal is its proposal manager. A proposal
needs at least one to be submitted and keeps at least one; the manager role is
granted and revoked by the proposal's managers and by whoever oversees the
call. Once submitted, the team is frozen for the applicant, and every change
an overseer or staff makes is logged on the proposal.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions import tasks as permissions_tasks
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models as users_models
from waldur_core.users.tests import factories as users_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures

MANAGER = RoleEnum.PROPOSAL_MANAGER
MEMBER = RoleEnum.PROPOSAL_MEMBER


class ProposalTeamMixin:
    def setUp(self):
        super().setUp()
        # Mirrors permissions.yaml.
        for permission in (
            PermissionEnum.MANAGE_PROPOSAL,
            PermissionEnum.UPDATE_PROPOSAL,
            PermissionEnum.UPDATE_PROPOSAL_PERMISSION,
            PermissionEnum.DELETE_PROPOSAL_PERMISSION,
        ):
            ProposalRole.MANAGER.add_permission(permission)
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.applicant = self.proposal.created_by
        self.proposal.add_user(self.applicant, ProposalRole.MANAGER)
        self.member = structure_factories.UserFactory(email="member@example.com")
        self.proposal.add_user(self.member, ProposalRole.MEMBER)
        self.other_member = structure_factories.UserFactory()
        self.proposal.add_user(self.other_member, ProposalRole.MEMBER)
        self.staff = structure_factories.UserFactory(is_staff=True)

    def _url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def _post(self, action, user, role, as_user, **extra):
        self.client.force_authenticate(as_user)
        return self.client.post(
            self._url(action), {"user": user.uuid.hex, "role": role, **extra}
        )

    def _grant(self, user, role, as_user=None):
        return self._post("add_user", user, role, as_user or self.applicant)

    def _revoke(self, user, role, as_user=None):
        return self._post("delete_user", user, role, as_user or self.applicant)

    def _update(self, user, role, as_user=None):
        return self._post(
            "update_user",
            user,
            role,
            as_user or self.applicant,
            expiration_time="2099-01-01",
        )

    def _submit_proposal(self):
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()

    def _is_manager(self, user):
        return self.proposal.has_user(user, ProposalRole.MANAGER)


class SubmissionNeedsManagerTest(ProposalTeamMixin, test.APITestCase):
    def _drop_all_managers(self):
        self.proposal.remove_user(self.applicant, ProposalRole.MANAGER)

    def test_can_submit_explains_a_missing_manager(self):
        self._drop_all_managers()
        can_submit, reason = self.proposal.can_submit()
        self.assertFalse(can_submit)
        self.assertEqual(reason, str(models.MISSING_PROPOSAL_MANAGER_MESSAGE))

    def test_can_submit_with_a_manager(self):
        self.assertEqual(self.proposal.can_submit(), (True, None))

    def test_expired_manager_does_not_count(self):
        self.proposal.remove_user(self.applicant, ProposalRole.MANAGER)
        self.proposal.add_user(self.member, ProposalRole.MANAGER)
        self.proposal.remove_user(self.member, ProposalRole.MANAGER)
        self.assertFalse(self.proposal.can_submit()[0])

    def test_serialized_can_submit_reports_a_missing_manager(self):
        self._drop_all_managers()
        self.client.force_authenticate(self.staff)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["can_submit"],
            {
                "can_submit": False,
                "error": str(models.MISSING_PROPOSAL_MANAGER_MESSAGE),
            },
        )

    def test_submit_is_refused_without_a_manager(self):
        self._drop_all_managers()
        self.client.force_authenticate(self.applicant)
        response = self.client.post(self._url("submit"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(str(models.MISSING_PROPOSAL_MANAGER_MESSAGE), str(response.data))


class LastManagerTest(ProposalTeamMixin, test.APITestCase):
    def test_staff_cannot_revoke_the_last_manager(self):
        response = self._revoke(self.applicant, MANAGER, as_user=self.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self._is_manager(self.applicant))

    def test_call_manager_cannot_revoke_the_last_manager(self):
        response = self._revoke(
            self.applicant, MANAGER, as_user=self.fixture.call_manager
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self._is_manager(self.applicant))

    def test_a_manager_may_be_revoked_while_another_remains(self):
        self.proposal.add_user(self.member, ProposalRole.MANAGER)
        response = self._revoke(self.applicant, MANAGER, as_user=self.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self._is_manager(self.applicant))

    def test_revoking_other_roles_is_not_affected(self):
        response = self._revoke(self.member, MEMBER, as_user=self.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class WhoManagesManagersTest(ProposalTeamMixin, test.APITestCase):
    """On a draft, the proposal manager role is granted and revoked by the
    proposal's managers, by whoever oversees the call, and by staff."""

    def test_manager_may_grant_and_revoke_manager(self):
        response = self._grant(self.member, MANAGER)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self._revoke(self.member, MANAGER)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self._is_manager(self.member))

    def test_manager_cannot_revoke_themselves(self):
        self.proposal.add_user(self.member, ProposalRole.MANAGER)
        response = self._revoke(self.applicant, MANAGER)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self._is_manager(self.applicant))

    def test_member_cannot_grant_manager(self):
        response = self._grant(self.other_member, MANAGER, as_user=self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_cannot_revoke_manager(self):
        self.proposal.add_user(self.other_member, ProposalRole.MANAGER)
        response = self._revoke(self.other_member, MANAGER, as_user=self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_team_permission_without_the_manager_role_cannot_grant_manager(self):
        # A role carrying the team permission manages the rest of the team,
        # but not who manages the proposal.
        editor_role = Role.objects.create(
            name="PROPOSAL.EDITOR",
            content_type=ProposalRole.MANAGER.content_type,
        )
        editor_role.add_permission(PermissionEnum.MANAGE_PROPOSAL)
        editor = structure_factories.UserFactory()
        self.proposal.add_user(editor, editor_role)

        response = self._grant(self.other_member, MANAGER, as_user=editor)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self._is_manager(self.other_member))

        outsider = structure_factories.UserFactory()
        response = self._grant(outsider, MEMBER, as_user=editor)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_call_manager_may_grant_and_revoke_manager_on_a_draft(self):
        call_manager = self.fixture.call_manager
        response = self._grant(self.member, MANAGER, as_user=call_manager)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self._revoke(self.applicant, MANAGER, as_user=call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self._is_manager(self.applicant))
        self.assertTrue(self._is_manager(self.member))

    def test_call_organiser_may_grant_and_revoke_manager_on_a_draft(self):
        organiser = self.fixture.call_organizer_user
        response = self._grant(self.member, MANAGER, as_user=organiser)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self._revoke(self.applicant, MANAGER, as_user=organiser)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self._is_manager(self.member))

    def test_call_manager_manages_members_on_a_draft(self):
        call_manager = self.fixture.call_manager
        outsider = structure_factories.UserFactory()
        response = self._grant(outsider, MEMBER, as_user=call_manager)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self._revoke(self.member, MEMBER, as_user=call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_manager_removes_and_re_roles_a_member(self):
        response = self._update(self.member, MEMBER)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._revoke(self.member, MEMBER)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self.proposal.has_user(self.member, ProposalRole.MEMBER))
        response = self._grant(self.member, RoleEnum.PROPOSAL_ADMIN)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_member_cannot_manage_the_team(self):
        outsider = structure_factories.UserFactory()
        self.assertEqual(
            self._grant(outsider, MEMBER, as_user=self.member).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self._revoke(self.other_member, MEMBER, as_user=self.member).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_call_manager_cannot_make_themselves_manager(self):
        call_manager = self.fixture.call_manager
        response = self._grant(call_manager, MANAGER, as_user=call_manager)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reviewer_cannot_grant_manager(self):
        response = self._grant(self.member, MANAGER, as_user=self.fixture.reviewer_1)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_staff_may_grant_manager(self):
        response = self._grant(self.member, MANAGER, as_user=self.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_overseer_helper(self):
        helper = proposal_permissions.oversees_proposal_call
        self.assertTrue(helper(self.staff, self.proposal))
        self.assertTrue(helper(self.fixture.call_manager, self.proposal))
        self.assertTrue(helper(self.fixture.call_organizer_user, self.proposal))
        self.assertFalse(helper(self.fixture.reviewer_1, self.proposal))
        self.assertFalse(helper(self.applicant, self.proposal))
        self.assertFalse(helper(None, self.proposal))


class ManagerInvitationTest(ProposalTeamMixin, test.APITestCase):
    def _invite(self, role, as_user):
        self.client.force_authenticate(as_user)
        return self.client.post(
            users_factories.InvitationBaseFactory.get_list_url(),
            {
                "email": "invitee@example.com",
                "scope": factories.ProposalFactory.get_url(self.proposal),
                "role": role.uuid.hex,
            },
        )

    def _invitation(self, role, created_by):
        return users_models.Invitation.objects.create(
            scope=self.proposal,
            customer=self.proposal.project.customer,
            role=role,
            email="invitee@example.com",
            created_by=created_by,
        )

    def _accept(self, invitation):
        invitee = structure_factories.UserFactory(email=invitation.email)
        self.client.force_authenticate(invitee)
        response = self.client.post(
            users_factories.InvitationBaseFactory.get_url(invitation, "accept")
        )
        return invitee, response

    def test_manager_may_invite_a_manager(self):
        response = self._invite(ProposalRole.MANAGER, self.applicant)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_team_permission_without_the_manager_role_cannot_invite_a_manager(self):
        editor_role = Role.objects.create(
            name="PROPOSAL.EDITOR",
            content_type=ProposalRole.MANAGER.content_type,
        )
        editor_role.add_permission(PermissionEnum.MANAGE_PROPOSAL)
        editor = structure_factories.UserFactory()
        self.proposal.add_user(editor, editor_role)

        response = self._invite(ProposalRole.MANAGER, editor)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invitation_by_a_former_manager_cannot_be_accepted(self):
        former = structure_factories.UserFactory()
        invitation = self._invitation(ProposalRole.MANAGER, former)
        invitee, response = self._accept(invitation)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self._is_manager(invitee))

    def test_invitation_by_a_manager_is_accepted(self):
        invitation = self._invitation(ProposalRole.MANAGER, self.applicant)
        invitee, response = self._accept(invitation)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self._is_manager(invitee))

    def test_call_manager_invites_on_a_draft(self):
        response = self._invite(ProposalRole.MEMBER, self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_call_overseers_invite_after_submission(self):
        self._submit_proposal()
        for overseer in (self.fixture.call_manager, self.fixture.call_organizer_user):
            response = self._invite(ProposalRole.MANAGER, overseer)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )
            users_models.Invitation.objects.all().delete()

    def test_invitation_by_a_call_manager_is_accepted_after_submission(self):
        self._submit_proposal()
        invitation = self._invitation(ProposalRole.MEMBER, self.fixture.call_manager)
        invitee, response = self._accept(invitation)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self.proposal.has_user(invitee, ProposalRole.MEMBER))

    def test_reviewer_cannot_invite(self):
        response = self._invite(ProposalRole.MEMBER, self.fixture.reviewer_1)
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND),
        )
        self.assertFalse(users_models.Invitation.objects.exists())

    def test_invitations_are_refused_after_submission(self):
        self._submit_proposal()
        for role in (ProposalRole.MANAGER, ProposalRole.MEMBER):
            response = self._invite(role, self.applicant)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pending_invitation_cannot_be_accepted_after_submission(self):
        invitation = self._invitation(ProposalRole.MEMBER, self.applicant)
        self._submit_proposal()
        invitee, response = self._accept(invitation)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.proposal.has_user(invitee))

    def test_permission_request_for_manager_needs_a_manager_to_approve(self):
        group_invitation = users_models.GroupInvitation.objects.create(
            scope=self.proposal,
            customer=self.proposal.project.customer,
            role=ProposalRole.MANAGER,
            created_by=self.applicant,
        )
        requester = structure_factories.UserFactory()
        permission_request = users_models.PermissionRequest.objects.create(
            invitation=group_invitation, created_by=requester
        )
        with self.assertRaises(ValidationError):
            permission_request.approve(self.member, "")
        self.assertFalse(self._is_manager(requester))

        permission_request.refresh_from_db()
        permission_request.approve(self.applicant, "")
        self.assertTrue(self._is_manager(requester))


class TeamFrozenAfterSubmissionTest(ProposalTeamMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.co_manager = structure_factories.UserFactory()
        self.proposal.add_user(self.co_manager, ProposalRole.MANAGER)
        self._submit_proposal()

    def test_manager_cannot_change_managers(self):
        self.assertEqual(
            self._grant(self.member, MANAGER).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self._revoke(self.co_manager, MANAGER).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertTrue(self._is_manager(self.co_manager))

    def test_manager_cannot_change_members(self):
        outsider = structure_factories.UserFactory()
        response = self._grant(outsider, MEMBER)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.proposal.has_user(outsider))

    def test_staff_may_change_the_team(self):
        self.assertEqual(
            self._revoke(self.member, MEMBER, as_user=self.staff).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self._grant(self.member, MANAGER, as_user=self.staff).status_code,
            status.HTTP_201_CREATED,
        )

    def test_call_manager_may_change_managers(self):
        call_manager = self.fixture.call_manager
        self.assertEqual(
            self._grant(self.member, MANAGER, as_user=call_manager).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self._revoke(self.co_manager, MANAGER, as_user=call_manager).status_code,
            status.HTTP_200_OK,
        )

    def test_call_organiser_may_change_managers(self):
        response = self._grant(
            self.member, MANAGER, as_user=self.fixture.call_organizer_user
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_call_manager_may_change_members(self):
        response = self._revoke(self.member, MEMBER, as_user=self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self.proposal.has_user(self.member, ProposalRole.MEMBER))

    def test_manager_cannot_remove_members(self):
        response = self._revoke(self.member, MEMBER)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self.proposal.has_user(self.member, ProposalRole.MEMBER))


class TeamChangeAfterSubmissionAuditTest(ProposalTeamMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call_manager = self.fixture.call_manager
        self._submit_proposal()

    def _events(self):
        return Event.objects.filter(
            event_type=EventType.PROPOSAL_TEAM_CHANGED_AFTER_SUBMISSION
        ).order_by("created")

    def test_call_manager_change_is_logged(self):
        response = self._grant(self.member, MANAGER, as_user=self.call_manager)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self._revoke(self.applicant, MANAGER, as_user=self.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        granted, revoked = self._events()
        context = granted.context
        self.assertEqual(context["change"], "granted")
        self.assertEqual(context["role_name"], MANAGER)
        self.assertEqual(context["proposal_uuid"], self.proposal.uuid.hex)
        self.assertEqual(context["proposal_state"], ProposalStates.SUBMITTED)
        self.assertEqual(context["affected_user_uuid"], self.member.uuid.hex)
        self.assertEqual(context["actor_uuid"], self.call_manager.uuid.hex)
        self.assertIn(self.proposal.name, granted.message)
        self.assertIn("Proposal manager", granted.message)
        self.assertNotIn(MANAGER, granted.message)
        self.assertEqual(revoked.context["change"], "revoked")
        self.assertEqual(revoked.context["affected_user_uuid"], self.applicant.uuid.hex)

    def test_event_is_filed_on_the_proposal_feed(self):
        self._grant(self.member, MANAGER, as_user=self.call_manager)
        (event,) = self._events()
        self.assertEqual(
            [feed.scope for feed in Feed.objects.filter(event=event)],
            [self.proposal],
        )

    def test_call_manager_removing_a_member_is_logged(self):
        response = self._revoke(self.member, MEMBER, as_user=self.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        (event,) = self._events()
        self.assertEqual(event.context["change"], "revoked")
        self.assertEqual(event.context["role_name"], MEMBER)
        self.assertEqual(event.context["affected_user_uuid"], self.member.uuid.hex)
        self.assertEqual(event.context["actor_uuid"], self.call_manager.uuid.hex)

    def test_staff_member_changes_are_logged(self):
        self._update(self.member, MEMBER, as_user=self.staff)
        self._revoke(self.member, MEMBER, as_user=self.staff)
        updated, revoked = self._events()
        self.assertEqual(updated.context["change"], "updated")
        self.assertEqual(revoked.context["change"], "revoked")
        self.assertEqual(revoked.context["role_name"], MEMBER)
        self.assertEqual(revoked.context["actor_uuid"], self.staff.uuid.hex)

    def test_accepted_invitation_is_logged_on_the_inviters_authority(self):
        invitee = structure_factories.UserFactory(email="late@example.com")
        invitation = users_models.Invitation.objects.create(
            scope=self.proposal,
            customer=self.proposal.project.customer,
            role=ProposalRole.MANAGER,
            email=invitee.email,
            created_by=self.call_manager,
        )
        self.client.force_authenticate(invitee)
        response = self.client.post(
            users_factories.InvitationBaseFactory.get_url(invitation, "accept")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        (event,) = self._events()
        self.assertEqual(event.context["affected_user_uuid"], invitee.uuid.hex)
        self.assertEqual(event.context["actor_uuid"], self.call_manager.uuid.hex)

    def test_approved_permission_request_is_logged_with_the_approver(self):
        requester = structure_factories.UserFactory()
        group_invitation = users_models.GroupInvitation.objects.create(
            scope=self.proposal,
            customer=self.proposal.project.customer,
            role=ProposalRole.MANAGER,
            created_by=self.call_manager,
        )
        permission_request = users_models.PermissionRequest.objects.create(
            invitation=group_invitation, created_by=requester
        )
        permission_request.approve(self.call_manager, "")

        (event,) = self._events()
        self.assertEqual(event.context["affected_user_uuid"], requester.uuid.hex)
        self.assertEqual(event.context["actor_uuid"], self.call_manager.uuid.hex)

    def test_draft_changes_are_not_logged(self):
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.assertEqual(
            self._grant(self.member, MANAGER).status_code, status.HTTP_201_CREATED
        )
        self.assertFalse(self._events().exists())


class ExpiryKeepsLastManagerTest(ProposalTeamMixin, test.APITestCase):
    def _expire(self, user, role):
        UserRole.objects.filter(scope=self.proposal, user=user, role=role).update(
            expiration_time=timezone.now() - timedelta(minutes=1)
        )

    def test_last_manager_is_not_expired(self):
        self._expire(self.applicant, ProposalRole.MANAGER)
        permissions_tasks.check_expired_permissions()
        self.assertTrue(self._is_manager(self.applicant))

    def test_one_of_two_expiring_managers_is_kept(self):
        self.proposal.add_user(self.member, ProposalRole.MANAGER)
        self._expire(self.applicant, ProposalRole.MANAGER)
        self._expire(self.member, ProposalRole.MANAGER)
        permissions_tasks.check_expired_permissions()
        self.assertEqual(
            UserRole.objects.filter(
                scope=self.proposal, is_active=True, role=ProposalRole.MANAGER
            ).count(),
            1,
        )

    def test_manager_expires_while_another_remains(self):
        self.proposal.add_user(self.member, ProposalRole.MANAGER)
        self._expire(self.applicant, ProposalRole.MANAGER)
        permissions_tasks.check_expired_permissions()
        self.assertFalse(self._is_manager(self.applicant))

    def test_other_draft_roles_still_expire(self):
        self._expire(self.member, ProposalRole.MEMBER)
        permissions_tasks.check_expired_permissions()
        self.assertFalse(self.proposal.has_user(self.member, ProposalRole.MEMBER))


class OwnProposalRoleTest(ProposalTeamMixin, test.APITestCase):
    """Nobody changes their own proposal role, staff included."""

    def setUp(self):
        super().setUp()
        self.proposal.add_user(self.staff, ProposalRole.MEMBER)
        # Another manager remains, so the last-manager rule is not what refuses.
        self.proposal.add_user(self.member, ProposalRole.MANAGER)

    def _assert_refused_in_any_state(self, check):
        check()
        self._submit_proposal()
        check()

    def test_staff_cannot_grant_themselves_a_role(self):
        def check():
            response = self._grant(self.staff, MANAGER, as_user=self.staff)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertFalse(self._is_manager(self.staff))

        self._assert_refused_in_any_state(check)

    def test_staff_cannot_revoke_their_own_role(self):
        def check():
            response = self._revoke(self.staff, MEMBER, as_user=self.staff)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertTrue(self.proposal.has_user(self.staff, ProposalRole.MEMBER))

        self._assert_refused_in_any_state(check)

    def test_staff_creating_a_proposal_becomes_its_manager(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.ProposalFactory.get_list_url(),
            {
                "name": "Staff proposal",
                "round_uuid": self.fixture.round.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        proposal = models.Proposal.objects.get(uuid=response.data["uuid"])
        self.assertTrue(proposal.has_user(self.staff, ProposalRole.MANAGER))
