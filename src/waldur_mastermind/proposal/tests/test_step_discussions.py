"""Tracked discussions of workflow steps: an encrypted Matrix room for the
people working on a step, opened by one of them and closed, its history
exported, when the step ends."""

import json
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.test_access_coverage import unexplained_routes
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, OfferingRole, ProposalRole
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models as matrix_models
from waldur_mastermind.matrix_chat import tasks as matrix_tasks
from waldur_mastermind.proposal import (
    call_templates,
    call_transfer,
    models,
    proposal_roles,
    step_discussions,
    utils,
    workflow_checks,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    COIStatuses,
    ContributorSources,
    OutcomeSets,
    ProposalRoleEligibility,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_round_steps import RoundStepTestBase

MATRIX = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
)
MATRIX_OFF = dict(MATRIX_ENABLED=False)

LIST_URL = "http://testserver" + reverse("proposal-step-discussion-list")
OPEN_URL = LIST_URL + "open/"

# The homeserver side of a room is Celery's; these tests stop at asking.
CREATE_ROOM = "waldur_mastermind.matrix_chat.tasks.create_room.delay"
SYNC_ROOM = "waldur_mastermind.matrix_chat.tasks.sync_room_members.delay"
DISABLE_ROOM = "waldur_mastermind.matrix_chat.tasks.disable_room.delay"
RESYNC = "waldur_mastermind.matrix_chat.tasks.resync_scope_rooms"


def detail_url(discussion):
    return "http://testserver" + reverse(
        "proposal-step-discussion-detail", kwargs={"uuid": discussion.uuid.hex}
    )


class DiscussionTestBase(test.APITestCase):
    """A call whose first step is an assessment two rapporteurs -- holders
    of an evaluator role -- contribute to and complete, with a discussion;
    then the allocation decision. One proposal, at the assessment."""

    def setUp(self):
        for permission in (
            PermissionEnum.LIST_CALLS,
            PermissionEnum.LIST_PROPOSALS,
            PermissionEnum.UPDATE_CALL,
        ):
            CallRole.MANAGER.add_permission(permission)
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        self.call.workflow_steps.all().delete()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.alice = self.panel_member()
        self.bob = self.panel_member()
        self.outsider = self.panel_member()
        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.CALL_PANEL_MEMBER,
            min_holders=1,
            max_holders=2,
        )
        self.assessment = self.add_step(
            "assessment",
            StepKinds.ASSESSMENT,
            0,
            outcome_set=OutcomeSets.DONE,
            contributor_source=f"{ContributorSources.PROPOSAL_ROLE}:{self.role.pk}",
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
            has_discussion=True,
        )
        self.decision = self.add_step(
            "allocation_decision",
            StepKinds.DECISION,
            1,
            outcome_set=OutcomeSets.APPROVAL,
            completer=ResponsibleRoles.CALL_MANAGER,
            is_allocation_decision=True,
        )
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now + timedelta(days=10),
        )
        self.proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        self.applicant = self.proposal.created_by
        self.proposal.add_user(self.applicant, ProposalRole.MANAGER)
        for user in (self.alice, self.bob):
            models.ProposalRoleAssignment.objects.create(
                proposal=self.proposal, role=self.role, user=user
            )
        with transaction.atomic():
            workflow_service.create_step_instances(self.proposal)
            workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()
        self.instance = self.proposal.workflow_step_instances.get(
            call_step=self.assessment
        )

    def panel_member(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.PANEL_MEMBER)
        return user

    def add_step(self, key, kind, order, **values):
        return models.CallWorkflowStep.objects.create(
            call=self.call, key=key, kind=kind, name=key.title(), order=order, **values
        )

    def participants(self, **target):
        target = target or {"proposal_step": self.instance}
        return step_discussions.participant_ids(models.StepDiscussion(**target))

    def open(self, user, **data):
        self.client.force_authenticate(user)
        data = data or {"proposal_step_uuid": self.instance.uuid.hex}
        with mock.patch(CREATE_ROOM), mock.patch(SYNC_ROOM):
            return self.client.post(OPEN_URL, data, format="json")

    def open_discussion(self, user=None):
        response = self.open(user or self.alice)
        self.assertIn(
            response.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED)
        )
        return models.StepDiscussion.objects.get(uuid=response.data["uuid"])

    def activate_room(self, discussion, *members):
        room = step_discussions.room_of(discussion)
        room.state = matrix_models.RoomStates.ACTIVE
        room.room_id = f"!{discussion.pk}:matrix.example.com"
        room.save(update_fields=["state", "room_id"])
        for user in members:
            matrix_models.MatrixRoomMember.objects.create(
                room=room,
                user=user,
                matrix_user_id=f"@{user.username}:matrix.example.com",
                membership_state=matrix_models.MembershipStates.JOINED,
            )
        return room

    def complete_step(self, user=None):
        self.instance.refresh_from_db()
        with transaction.atomic():
            workflow_service.complete_step(
                self.proposal,
                self.instance,
                "done",
                "",
                completed_by=user or self.alice,
            )
        self.instance.refresh_from_db()

    def step_url(self, step):
        return (
            factories.CallFactory.get_protected_url(self.call, action="workflow_steps")
            + step.uuid.hex
            + "/"
        )


# =============================================================================
# The step's settings
# =============================================================================


class StepSettingsTest(DiscussionTestBase):
    def patch_step(self, step, data):
        self.client.force_authenticate(self.manager)
        return self.client.patch(self.step_url(step), data, format="json")

    def test_off_by_default(self):
        step = self.add_step("check", StepKinds.DECISION, 5)
        self.assertFalse(step.has_discussion)
        self.assertTrue(step.discussion_call_managers)
        self.assertFalse(step.discussion_panel_members)

    @override_config(**MATRIX)
    def test_call_manager_switches_it_on_while_matrix_is_enabled(self):
        step = self.add_step("review", StepKinds.ASSESSMENT, 5)
        response = self.patch_step(
            step, {"has_discussion": True, "discussion_panel_members": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        step.refresh_from_db()
        self.assertTrue(step.has_discussion)
        self.assertTrue(step.discussion_panel_members)

    @override_config(**MATRIX_OFF)
    def test_refused_while_matrix_is_disabled(self):
        step = self.add_step("review", StepKinds.ASSESSMENT, 5)
        response = self.patch_step(step, {"has_discussion": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("has_discussion", response.data)

    @override_config(**MATRIX_OFF)
    def test_a_step_keeps_its_discussion_when_matrix_is_disabled_later(self):
        response = self.patch_step(self.assessment, {"name": "Assessment, renamed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assessment.refresh_from_db()
        self.assertTrue(self.assessment.has_discussion)

    @override_config(**MATRIX)
    def test_refused_on_a_blind_step(self):
        step = self.add_step("review", StepKinds.ASSESSMENT, 5, blind_review=True)
        response = self.patch_step(step, {"has_discussion": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("has_discussion", response.data)

    @override_config(**MATRIX)
    def test_a_step_with_a_discussion_cannot_turn_blind(self):
        response = self.patch_step(self.assessment, {"blind_review": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assessment.refresh_from_db()
        self.assertFalse(self.assessment.blind_review)

    @override_config(**MATRIX)
    def test_refused_on_an_applicant_step(self):
        step = self.add_step(
            "answer",
            StepKinds.APPLICANT_ACTION,
            5,
            outcome_set=OutcomeSets.SUBMITTED,
            completer=ResponsibleRoles.APPLICANT,
        )
        response = self.patch_step(step, {"has_discussion": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("has_discussion", response.data)

    def test_model_refuses_blind_and_applicant_steps(self):
        self.assessment.blind_review = True
        with self.assertRaises(DjangoValidationError):
            self.assessment.clean()

    def test_kinds_offer_the_settings_but_the_applicant_step(self):
        for kind in (StepKinds.ASSESSMENT, StepKinds.DECISION, StepKinds.ASSIGNMENT):
            fields = models.CallWorkflowStep(kind=kind).kind_definition
            self.assertIn("has_discussion", fields.config_fields)
        applicant = models.CallWorkflowStep(
            kind=StepKinds.APPLICANT_ACTION
        ).kind_definition
        self.assertNotIn("has_discussion", applicant.config_fields)


class TemplatesAndTransferTest(DiscussionTestBase):
    def test_shipped_templates_have_no_discussion(self):
        directory = Path(call_templates.__file__).parent
        for path in directory.glob("*.json"):
            document = json.loads(path.read_text())
            for step in document.get("workflow_steps", []):
                self.assertFalse(
                    step.get("has_discussion", False), f"{path.name}: {step['key']}"
                )

    def import_call(self):
        self.assessment.discussion_call_managers = False
        self.assessment.discussion_panel_members = True
        self.assessment.save()
        document, _ = call_transfer.export_call(self.call)
        exported = next(
            s for s in document["workflow_steps"] if s["key"] == "assessment"
        )
        self.assertTrue(exported["has_discussion"])
        call, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.manager, name="Imported call"
        )
        return call.workflow_steps.get(key="assessment"), warnings

    @override_config(**MATRIX)
    def test_transfer_round_trips_the_settings(self):
        step, _ = self.import_call()
        self.assertTrue(step.has_discussion)
        self.assertFalse(step.discussion_call_managers)
        self.assertTrue(step.discussion_panel_members)

    @override_config(**MATRIX_OFF)
    def test_import_drops_the_discussion_while_matrix_is_disabled(self):
        step, warnings = self.import_call()
        self.assertFalse(step.has_discussion)
        self.assertTrue(any("has_discussion" in warning for warning in warnings))

    def test_a_copy_has_it_only_while_matrix_is_enabled(self):
        staff = structure_factories.UserFactory(is_staff=True)
        for settings, expected in ((MATRIX, True), (MATRIX_OFF, False)):
            with override_config(**settings):
                copy = utils.duplicate_call(self.call, "Copy", staff)
            self.assertEqual(
                copy.workflow_steps.get(key="assessment").has_discussion, expected
            )

    @override_config(**MATRIX_OFF)
    def test_workflow_checks_warn_without_matrix(self):
        codes = {f["code"] for f in workflow_checks.check_call(self.call)}
        self.assertIn("discussion_without_matrix", codes)

    @override_config(**MATRIX)
    def test_workflow_checks_refuse_a_blind_discussion(self):
        document = workflow_checks.call_document(self.call)
        step = next(s for s in document["workflow_steps"] if s["key"] == "assessment")
        step["blind_review"] = True
        codes = {f["code"] for f in workflow_checks.check(document)}
        self.assertIn("discussion_not_allowed", codes)
        self.assertNotIn("discussion_without_matrix", codes)

    @override_config(**MATRIX)
    def test_workflow_checks_refuse_a_discussion_of_offering_teams(self):
        for field, value in (
            ("per_requested_offering", True),
            ("contributor_source", ContributorSources.REQUESTED_OFFERING_TEAM),
            ("completer", ResponsibleRoles.OFFERING_MANAGER),
        ):
            document = workflow_checks.call_document(self.call)
            step = next(
                s for s in document["workflow_steps"] if s["key"] == "assessment"
            )
            step[field] = value
            findings = [
                f
                for f in workflow_checks.check(document)
                if f["code"] == "discussion_not_allowed"
            ]
            self.assertEqual(len(findings), 1, field)


# =============================================================================
# Who takes part
# =============================================================================


class ParticipantsTest(DiscussionTestBase):
    def test_contributors_completers_and_call_managers(self):
        self.assertEqual(
            self.participants(), {self.alice.pk, self.bob.pk, self.manager.pk}
        )

    def test_never_the_applicant(self):
        self.assertNotIn(self.applicant.pk, self.participants())

    def test_call_managers_only_when_the_step_includes_them(self):
        self.assessment.discussion_call_managers = False
        self.assessment.save()
        self.instance.refresh_from_db()
        self.assertEqual(self.participants(), {self.alice.pk, self.bob.pk})

    def test_a_withdrawn_contribution_drops_its_holder(self):
        self.assessment.discussion_call_managers = False
        self.assessment.save()
        models.ProposalRoleAssignment.objects.filter(user=self.bob).update(
            revoked_at=timezone.now()
        )
        self.instance.contributions.filter(contributor=self.bob).update(
            status=StepContributionStatuses.WITHDRAWN
        )
        self.instance.refresh_from_db()
        self.assertEqual(self.participants(), {self.alice.pk})

    def test_panel_members_when_the_step_includes_them(self):
        chair, member = self.panel_member(), self.panel_member()
        panel = models.CallPanel.objects.create(
            call=self.call, name="Physics", chair=chair
        )
        panel.members.add(chair, member)
        models.Proposal.objects.filter(pk=self.proposal.pk).update(panel=panel)
        self.assertNotIn(member.pk, self.participants())

        self.assessment.discussion_panel_members = True
        self.assessment.save()
        self.instance = models.ProposalWorkflowStepInstance.objects.get(
            pk=self.instance.pk
        )
        participants, moderators = step_discussions.people(
            models.StepDiscussion(proposal_step=self.instance)
        )
        self.assertTrue({chair.pk, member.pk} <= participants)
        self.assertNotIn(self.outsider.pk, participants)
        # The step's completers and the panel's chair moderate.
        self.assertEqual(moderators, {self.alice.pk, self.bob.pk, chair.pk})

    def test_a_recorded_conflict_keeps_its_holder_out(self):
        profile = factories.ReviewerProfileFactory(user=self.bob)
        factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.proposal, status=COIStatuses.PENDING
        )
        self.assertNotIn(self.bob.pk, self.participants())
        self.assertIn(self.alice.pk, self.participants())

    def test_the_applicant_team_is_kept_out_whatever_its_role(self):
        self.proposal.add_user(self.manager, ProposalRole.MEMBER)
        self.assertNotIn(self.manager.pk, self.participants())


# =============================================================================
# Opening
# =============================================================================


@override_config(**MATRIX)
class OpenTest(DiscussionTestBase):
    def test_a_participant_opens_it_once_and_later_opens_return_it(self):
        with mock.patch(CREATE_ROOM):
            first = self.open(self.alice)
            self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
            second = self.open(self.bob)
        self.assertEqual(second.status_code, status.HTTP_200_OK, second.data)
        self.assertEqual(first.data["uuid"], second.data["uuid"])
        self.assertEqual(models.StepDiscussion.objects.count(), 1)
        discussion = models.StepDiscussion.objects.get()
        self.assertEqual(discussion.opened_by, self.alice)
        room = step_discussions.room_of(discussion)
        self.assertEqual(first.data["room_uuid"], room.uuid.hex)
        self.assertIn(self.call.name, room.room_name)
        self.assertIn("Assessment", room.room_name)
        self.assertEqual(matrix_models.MatrixRoom.objects.count(), 1)

    def test_an_archived_call_opens_discussions_of_its_award_stage_only(self):
        models.Call.objects.filter(pk=self.call.pk).update(state=CallStates.ARCHIVED)
        response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        # The same step as award-stage work -- a final report's, say -- goes on.
        models.CallWorkflowStep.objects.filter(pk=self.assessment.pk).update(
            phase=WorkflowPhases.CLOSING
        )
        with mock.patch(CREATE_ROOM):
            response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        # A stranger is told nothing of the call.
        self.assertEqual(
            self.open(self.outsider).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_the_step_instance_says_who_may_open_it(self):
        self.client.force_authenticate(self.alice)
        url = factories.ProposalFactory.get_url(self.proposal, "workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = next(r for r in response.data if r["step"] == "assessment")
        self.assertEqual(
            row["discussion"], {"uuid": None, "state": "not_opened", "can_open": True}
        )
        decision = next(r for r in response.data if r["step"] == "allocation_decision")
        self.assertIsNone(decision["discussion"])

        discussion = self.open_discussion()
        self.client.force_authenticate(self.applicant)
        response = self.client.get(url)
        row = next(r for r in response.data if r["step"] == "assessment")
        self.assertIsNone(row["discussion"])

        self.client.force_authenticate(self.bob)
        response = self.client.get(url)
        row = next(r for r in response.data if r["step"] == "assessment")
        self.assertEqual(row["discussion"]["uuid"], discussion.uuid.hex)
        self.assertEqual(row["discussion"]["state"], "open")

    def test_refused_to_someone_not_working_on_the_step(self):
        for user in (self.outsider, self.applicant):
            response = self.open(user)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.StepDiscussion.objects.exists())

    def test_refused_on_a_step_without_discussion(self):
        self.assessment.has_discussion = False
        self.assessment.save()
        response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no discussion", str(response.data))

    @override_config(**MATRIX_OFF)
    def test_refused_while_matrix_is_disabled(self):
        response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Matrix chat is disabled", str(response.data))
        self.assertFalse(models.StepDiscussion.objects.exists())

    def test_refused_on_a_step_not_active(self):
        models.ProposalWorkflowStepInstance.objects.filter(pk=self.instance.pk).update(
            status=WorkflowStepInstanceStatuses.PENDING
        )
        response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_refused_once_closed(self):
        self.open_discussion()
        with mock.patch(DISABLE_ROOM):
            self.complete_step()
        response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("closed when the step ended", str(response.data))

    def test_names_one_step(self):
        self.client.force_authenticate(self.alice)
        response = self.client.post(OPEN_URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_routes_declare_who_may_call_them(self):
        undeclared, undescribed = unexplained_routes()
        mine = [
            route
            for route in undeclared + undescribed
            if "StepDiscussionViewSet" in route
        ]
        self.assertEqual(mine, [])


# =============================================================================
# Keeping the room in line
# =============================================================================


@override_config(**MATRIX)
class ResyncTest(DiscussionTestBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()

    def assert_resynced(self, action):
        with mock.patch(RESYNC) as resync:
            action()
        self.assertIn(
            self.discussion.pk, [call.args[0].pk for call in resync.call_args_list]
        )

    def test_on_a_contribution_slot_created_or_declined(self):
        contribution = self.instance.contributions.get(contributor=self.alice)
        self.assert_resynced(
            lambda: models.StepContribution.objects.filter(pk=contribution.pk)
            .first()
            .save()
        )
        self.assert_resynced(
            lambda: factories.StepContributionFactory(
                proposal_step=self.instance, contributor=self.outsider
            )
        )

    def test_on_a_role_revoked(self):
        assignment = models.ProposalRoleAssignment.objects.get(user=self.bob)
        self.assert_resynced(
            lambda: proposal_roles.revoke(assignment, revoked_by=self.manager)
        )

    def test_on_a_conflict_recorded(self):
        profile = factories.ReviewerProfileFactory(user=self.bob)
        self.assert_resynced(
            lambda: factories.ConflictOfInterestFactory(
                reviewer=profile, proposal=self.proposal
            )
        )

    def test_on_panel_membership_changed(self):
        panel = models.CallPanel.objects.create(call=self.call, name="Physics")
        models.Proposal.objects.filter(pk=self.proposal.pk).update(panel=panel)
        self.assessment.discussion_panel_members = True
        self.assessment.save()
        self.assert_resynced(lambda: panel.members.add(self.outsider))

    def test_on_a_call_manager_granted_or_revoked(self):
        user = structure_factories.UserFactory()
        self.assert_resynced(lambda: self.call.add_user(user, CallRole.MANAGER))
        self.assert_resynced(lambda: self.call.remove_user(user, CallRole.MANAGER))

    @override_config(**MATRIX_OFF)
    def test_nothing_while_matrix_is_disabled(self):
        with mock.patch(RESYNC) as resync:
            self.call.add_user(structure_factories.UserFactory(), CallRole.MANAGER)
        resync.assert_not_called()

    def test_member_sync_puts_the_participants_in_the_room(self):
        room = self.activate_room(self.discussion)
        with mock.patch(
            "waldur_mastermind.matrix_chat.tasks.matrix_client"
        ) as matrix_client:
            matrix_client.is_enabled.return_value = True
            matrix_client.ensure_user_exists.side_effect = (
                lambda user: f"@{user.username}:matrix.example.com"
            )
            matrix_client.get_power_level_for_scope.side_effect = (
                lambda user, scope: step_discussions.room_power_level(user, scope)
            )
            matrix_tasks.sync_room_members(str(room.uuid))
        members = {
            member.user_id: member.power_level
            for member in room.members.filter(
                membership_state=matrix_models.MembershipStates.JOINED
            )
        }
        self.assertEqual(
            members, {self.alice.pk: 50, self.bob.pk: 50, self.manager.pk: 0}
        )


# =============================================================================
# Closing
# =============================================================================


@override_config(**MATRIX)
class CloseTest(DiscussionTestBase):
    def test_closes_with_the_step_keeping_who_took_part(self):
        discussion = self.open_discussion()
        room = self.activate_room(discussion, self.alice, self.bob)
        callbacks = []
        with (
            mock.patch(
                "django.db.transaction.on_commit",
                side_effect=lambda func, using=None: callbacks.append(func),
            ),
            mock.patch(DISABLE_ROOM) as disable_room,
        ):
            self.complete_step()
            # Closed after the step completed, the room only once it commits.
            disable_room.assert_not_called()
            for callback in callbacks:
                callback()
        disable_room.assert_called_once_with(
            str(room.uuid),
            reason=step_discussions.ROOM_CLOSING_REASON,
            export_history=True,
        )
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)
        self.assertEqual(
            set(discussion.participants.values_list("id", flat=True)),
            {self.alice.pk, self.bob.pk, self.manager.pk},
        )
        room.refresh_from_db()
        self.assertEqual(room.state, matrix_models.RoomStates.DISABLING)
        # Who took part and is in it still belongs in it until it is
        # archived; nobody is brought in any more.
        self.assertEqual(
            set(step_discussions.room_members(discussion).values_list("id", flat=True)),
            {self.alice.pk, self.bob.pk},
        )

    def test_closes_when_the_proposal_is_withdrawn(self):
        discussion = self.open_discussion()
        self.activate_room(discussion)
        self.proposal.refresh_from_db()
        with mock.patch(DISABLE_ROOM):
            self.proposal.state = ProposalStates.CANCELED
            self.proposal.save()
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)

    def test_a_withdrawal_closes_it(self):
        discussion = self.open_discussion()
        self.proposal.refresh_from_db()
        with transaction.atomic():
            proposal = models.Proposal.objects.select_for_update().get(
                pk=self.proposal.pk
            )
            utils.withdraw_proposal(proposal, self.applicant)
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)

    def test_opening_locks_the_proposal_before_its_step(self):
        # The order a withdrawal locks them in, so that neither waits on the
        # other holding what it needs.
        with CaptureQueriesContext(connection) as context:
            self.open_discussion()
        locked = [
            q["sql"].split(" FROM ")[1].split()[0].strip('"')
            for q in context.captured_queries
            if "FOR UPDATE" in q["sql"]
        ]
        self.assertEqual(
            locked[:2],
            ["proposal_proposal", "proposal_proposalworkflowstepinstance"],
        )

    def test_a_step_without_one_asks_nothing(self):
        with mock.patch.object(step_discussions, "close_ended") as close_ended:
            self.complete_step()
        close_ended.assert_called_once()
        decision = self.proposal.workflow_step_instances.get(call_step=self.decision)
        with mock.patch.object(step_discussions, "close_ended") as close_ended:
            decision.status = WorkflowStepInstanceStatuses.COMPLETED
            decision.save()
        close_ended.assert_not_called()

    def test_room_still_being_created_is_not_disabled(self):
        discussion = self.open_discussion()
        with mock.patch(DISABLE_ROOM) as disable_room:
            self.complete_step()
        disable_room.assert_not_called()
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)
        self.assertTrue(discussion.is_removed)

    def test_the_export_is_on_the_record_for_the_call_managers(self):
        discussion = self.open_discussion()
        room = self.activate_room(discussion, self.alice)
        with mock.patch(DISABLE_ROOM):
            self.complete_step()
        export = matrix_models.MatrixHistoryExport.objects.create(
            room=room,
            export_type=matrix_models.ExportTypes.ON_DELETION,
            state=matrix_models.ExportStates.COMPLETED,
        )
        matrix_models.MatrixRoom.objects.filter(pk=room.pk).update(
            state=matrix_models.RoomStates.ARCHIVED
        )
        self.client.force_authenticate(self.manager)
        response = self.client.get(detail_url(discussion))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["state"], "closed")
        self.assertEqual(response.data["export_uuid"], export.uuid.hex)
        self.assertEqual(
            {p["uuid"] for p in response.data["participants"]},
            {self.alice.uuid.hex, self.bob.uuid.hex, self.manager.uuid.hex},
        )
        # Its download is the call managers'.
        response = self.client.get(
            "http://testserver"
            + reverse("matrix-export-detail", kwargs={"uuid": export.uuid.hex})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(self.alice)
        response = self.client.get(detail_url(discussion))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["export_uuid"])

    @mock.patch("waldur_mastermind.matrix_chat.tasks._export_before_closing")
    @mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
    @override_config(MATRIX_HISTORY_EXPORT_ENABLED=False)
    def test_disable_room_exports_when_asked_whatever_the_setting(
        self, matrix_client, export_before_closing
    ):
        discussion = self.open_discussion()
        room = self.activate_room(discussion)
        matrix_models.MatrixRoom.objects.filter(pk=room.pk).update(
            state=matrix_models.RoomStates.DISABLING
        )
        matrix_client.is_enabled.return_value = False
        export_before_closing.return_value = None
        matrix_tasks.disable_room(str(room.uuid), export_history=True)
        export_before_closing.assert_called_once()
        room.refresh_from_db()
        self.assertEqual(room.state, matrix_models.RoomStates.ARCHIVED)


class MatrixDisabledTest(DiscussionTestBase):
    def close_while_matrix_is_disabled(self):
        with override_config(**MATRIX):
            discussion = self.open_discussion()
            room = self.activate_room(discussion, self.alice, self.bob)
        with override_config(**MATRIX_OFF), mock.patch(DISABLE_ROOM):
            self.complete_step()
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)
        room.refresh_from_db()
        self.assertEqual(room.state, matrix_models.RoomStates.ACTIVE)
        return discussion, room

    @override_config(**MATRIX)
    def test_a_sync_of_a_closed_discussions_room_keeps_who_took_part(self):
        _, room = self.close_while_matrix_is_disabled()
        with mock.patch(
            "waldur_mastermind.matrix_chat.tasks.matrix_client"
        ) as matrix_client:
            matrix_client.is_enabled.return_value = True
            matrix_client.ensure_user_exists.side_effect = (
                lambda user: f"@{user.username}:matrix.example.com"
            )
            matrix_client.get_power_level_for_scope.return_value = 0
            matrix_tasks.sync_room_members(str(room.uuid))
        matrix_client.kick_user.assert_not_called()
        # The manager took part but was never in the room: not invited now.
        self.assertNotIn(
            f"@{self.manager.username}:matrix.example.com",
            [call.args[1] for call in matrix_client.invite_user.call_args_list],
        )
        self.assertEqual(
            set(
                room.members.filter(
                    membership_state=matrix_models.MembershipStates.JOINED
                ).values_list("user_id", flat=True)
            ),
            {self.alice.pk, self.bob.pk},
        )

    @override_config(**MATRIX, MATRIX_HISTORY_EXPORT_ENABLED=False)
    def test_its_room_is_exported_and_archived_once_matrix_is_back(self):
        _, room = self.close_while_matrix_is_disabled()
        with (
            mock.patch(DISABLE_ROOM) as disable_room,
            mock.patch(
                "waldur_mastermind.matrix_chat.tasks.export_room_history.delay"
            ) as periodic_export,
        ):
            matrix_tasks.periodic_history_export()
        disable_room.assert_called_once_with(
            str(room.uuid),
            reason=step_discussions.ROOM_CLOSING_REASON,
            export_history=True,
        )
        periodic_export.assert_not_called()
        room.refresh_from_db()
        self.assertEqual(room.state, matrix_models.RoomStates.DISABLING)

    def test_nothing_is_closed_while_matrix_is_still_disabled(self):
        _, room = self.close_while_matrix_is_disabled()
        with override_config(**MATRIX_OFF), mock.patch(DISABLE_ROOM) as disable_room:
            self.assertEqual(matrix_tasks.close_rooms_of_ended_scopes(), 0)
        disable_room.assert_not_called()
        room.refresh_from_db()
        self.assertEqual(room.state, matrix_models.RoomStates.ACTIVE)

    def test_records_stay_readable_and_close_without_a_room(self):
        with override_config(**MATRIX):
            discussion = self.open_discussion()
            room = self.activate_room(discussion, self.alice)
        with override_config(**MATRIX_OFF), mock.patch(DISABLE_ROOM) as disable_room:
            self.client.force_authenticate(self.alice)
            response = self.client.get(LIST_URL)
            self.assertEqual([d["uuid"] for d in response.data], [discussion.uuid.hex])
            self.complete_step()
        disable_room.assert_not_called()
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)
        room.refresh_from_db()
        self.assertEqual(room.state, matrix_models.RoomStates.ACTIVE)


# =============================================================================
# Who reads them
# =============================================================================


@override_config(**MATRIX)
class VisibilityTest(DiscussionTestBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()
        self.activate_room(self.discussion, self.alice, self.bob)

    def listed(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(LIST_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [d["uuid"] for d in response.data]

    def test_participants_and_call_managers_read_it(self):
        for user in (self.alice, self.bob, self.manager):
            self.assertEqual(self.listed(user), [self.discussion.uuid.hex])
        for user in (self.outsider, self.applicant):
            self.assertEqual(self.listed(user), [])
            self.client.force_authenticate(user)
            response = self.client.get(detail_url(self.discussion))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_names_the_step_and_the_room(self):
        self.client.force_authenticate(self.bob)
        data = self.client.get(detail_url(self.discussion)).data
        self.assertEqual(data["state"], "open")
        self.assertEqual(data["proposal_uuid"], self.proposal.uuid.hex)
        self.assertEqual(data["proposal_step_uuid"], self.instance.uuid.hex)
        self.assertEqual(data["step_name"], "Assessment")
        self.assertEqual(data["call_uuid"], self.call.uuid.hex)
        self.assertEqual(data["round_uuid"], self.round.uuid.hex)
        self.assertIsNone(data["round_step_uuid"])
        self.assertEqual(data["room_id"], f"!{self.discussion.pk}:matrix.example.com")
        self.assertIsNone(data["export_uuid"])

    def test_filters(self):
        uuid = self.discussion.uuid.hex
        self.assertEqual(self.listed(self.manager, mine=True), [])
        self.assertEqual(self.listed(self.alice, mine=True), [uuid])
        self.assertEqual(self.listed(self.alice, state="open"), [uuid])
        self.assertEqual(self.listed(self.alice, state="closed"), [])
        self.assertEqual(
            self.listed(self.alice, proposal_uuid=self.proposal.uuid.hex), [uuid]
        )
        self.assertEqual(self.listed(self.alice, call_uuid=self.call.uuid.hex), [uuid])
        self.assertEqual(
            self.listed(self.alice, round_uuid=self.round.uuid.hex), [uuid]
        )

    def test_who_took_part_reads_it_once_closed(self):
        with mock.patch(DISABLE_ROOM):
            self.complete_step()
        matrix_models.MatrixRoomMember.objects.update(
            membership_state=matrix_models.MembershipStates.LEFT
        )
        self.assertEqual(
            self.listed(self.bob, state="closed"), [self.discussion.uuid.hex]
        )
        self.assertEqual(self.listed(self.outsider), [])


# =============================================================================
# Round steps
# =============================================================================


class RoundDiscussionBase(RoundStepTestBase):
    """The round's domain panel meeting with a discussion; the first two
    proposals on chair A's panel, the third on chair B's."""

    def setUp(self):
        super().setUp()
        self.meeting.has_discussion = True
        self.meeting.save()
        self.panel_a = models.CallPanel.objects.create(
            call=self.call, name="Panel A", chair=self.chair_a
        )
        self.panel_a.members.add(self.chair_a)
        self.panel_b = models.CallPanel.objects.create(
            call=self.call, name="Panel B", chair=self.chair_b
        )
        self.panel_b.members.add(self.chair_b)
        models.Proposal.objects.filter(pk__in=[self.first.pk, self.second.pk]).update(
            panel=self.panel_a
        )
        models.Proposal.objects.filter(pk=self.third.pk).update(panel=self.panel_b)
        self.open_all()
        self.round_step = models.RoundStep.objects.get(
            round=self.round, call_step=self.meeting
        )

    def open_discussion(self, user, panel=None, **data):
        self.client.force_authenticate(user)
        data = data or {
            "round_step_uuid": self.round_step.uuid.hex,
            "panel_uuid": panel.uuid.hex if panel else None,
        }
        with mock.patch(CREATE_ROOM), mock.patch(SYNC_ROOM):
            return self.client.post(OPEN_URL, data, format="json")


@override_config(**MATRIX)
class RoundDiscussionTest(RoundDiscussionBase):
    def test_one_discussion_per_panel(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["panel_uuid"], self.panel_a.uuid.hex)
        discussion_a = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        participants = step_discussions.participant_ids(discussion_a)
        self.assertIn(self.chair_a.pk, participants)
        self.assertIn(self.manager.pk, participants)
        self.assertNotIn(self.chair_b.pk, participants)
        self.assertIn("Panel A", step_discussions.room_of(discussion_a).room_name)

        # Chair A does not sit on panel B's.
        response = self.open_discussion(self.chair_a, self.panel_b)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.open_discussion(self.chair_b, self.panel_b)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(response.data["uuid"], discussion_a.uuid.hex)

    def test_a_proposal_step_opens_the_round_discussion_of_its_panel(self):
        instance = self.instance(self.first)
        response = self.open_discussion(
            self.chair_a, proposal_step_uuid=instance.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["round_step_uuid"], self.round_step.uuid.hex)
        self.assertEqual(response.data["panel_uuid"], self.panel_a.uuid.hex)
        again = self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.data["uuid"], response.data["uuid"])

    def test_a_call_with_panels_names_the_panel(self):
        response = self.open_discussion(
            self.chair_a, round_step_uuid=self.round_step.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("panel_uuid", response.data)

    def test_the_round_step_lists_the_viewers_discussions(self):
        self.client.force_authenticate(self.manager)
        url = factories.RoundFactory.get_url(self.call, self.round)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = next(
            r for r in response.data["round_steps"] if r["step_key"] == "meeting"
        )
        self.assertEqual(row["round_step_uuid"], self.round_step.uuid.hex)
        self.assertTrue(row["has_discussion"])
        self.assertEqual(
            {d["panel_uuid"] for d in row["discussions"]},
            {self.panel_a.uuid.hex, self.panel_b.uuid.hex},
        )
        self.assertTrue(all(d["can_open"] for d in row["discussions"]))

    def test_closes_when_its_panels_proposals_are_done_with_the_step(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        discussion_a = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        response = self.open_discussion(self.chair_b, self.panel_b)
        discussion_b = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        for proposal in (self.first, self.second):
            instance = self.instance(proposal)
            discussion_a.refresh_from_db()
            self.assertIsNone(discussion_a.closed_at)
            instance.status = WorkflowStepInstanceStatuses.COMPLETED
            instance.save()
        discussion_a.refresh_from_db()
        discussion_b.refresh_from_db()
        self.assertIsNotNone(discussion_a.closed_at)
        self.assertIsNone(discussion_b.closed_at)
        self.assertIn(
            self.chair_a.pk, discussion_a.participants.values_list("id", flat=True)
        )

    def test_opening_locks_the_steps_that_keep_it_running(self):
        with CaptureQueriesContext(connection) as context:
            response = self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        locked = [
            q["sql"].split(" FROM ")[1].split()[0].strip('"')
            for q in context.captured_queries
            if "FOR UPDATE" in q["sql"]
        ]
        self.assertEqual(
            locked[:2], ["proposal_roundstep", "proposal_proposalworkflowstepinstance"]
        )

    def test_closing_locks_the_discussions_before_counting(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        discussion = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        with CaptureQueriesContext(connection) as context:
            step_discussions.close_finished_round(self.round.pk, self.meeting.pk)
        first = next(
            q["sql"] for q in context.captured_queries if "FOR UPDATE" in q["sql"]
        )
        self.assertIn("proposal_stepdiscussion", first.split(" FROM ")[1].split()[0])
        discussion.refresh_from_db()
        self.assertIsNone(discussion.closed_at)

    def test_one_open_per_panel_and_one_without_on_any_database(self):
        for panel in (self.panel_a, None):
            models.StepDiscussion.objects.create(
                round_step=self.round_step, panel=panel
            )
            with self.assertRaises(IntegrityError), transaction.atomic():
                models.StepDiscussion.objects.create(
                    round_step=self.round_step, panel=panel
                )
        # Closed ones stay on record beside the open one.
        models.StepDiscussion.objects.filter(round_step=self.round_step).update(
            closed_at=timezone.now()
        )
        models.StepDiscussion.objects.create(round_step=self.round_step, panel=None)

    def test_a_panel_that_held_one_is_not_deleted(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        models.Proposal.objects.filter(panel=self.panel_a).update(panel=None)
        self.client.force_authenticate(self.manager)
        url = (
            factories.CallFactory.get_protected_url(self.call, action="panels")
            + self.panel_a.uuid.hex
            + "/"
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("discussion", str(response.data))
        self.assertTrue(models.CallPanel.objects.filter(pk=self.panel_a.pk).exists())

    def test_round_room_name(self):
        discussion = models.StepDiscussion(
            round_step=self.round_step, panel=self.panel_b
        )
        self.assertEqual(
            step_discussions.discussion_name(discussion),
            f"{self.call.name} · Meeting · {self.round.name} · Panel B",
        )


class ExportReadersTest(DiscussionTestBase):
    def test_call_managers_read_the_exports_of_their_calls_discussions(self):
        with override_config(**MATRIX):
            discussion = self.open_discussion()
        self.assertIn(
            discussion.pk,
            step_discussions.export_reader_ids(self.manager).values_list(
                "id", flat=True
            ),
        )
        self.assertFalse(step_discussions.export_reader_ids(self.alice).exists())
        content_type = ContentType.objects.get_for_model(models.StepDiscussion)
        self.assertEqual(
            step_discussions.room_of(discussion).content_type, content_type
        )


# =============================================================================
# Deleted discussions, and users reactivated
# =============================================================================


@override_config(**MATRIX)
class DeletedDiscussionTest(DiscussionTestBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()
        self.room = self.activate_room(self.discussion, self.alice, self.bob)

    def test_its_room_is_exported_once_the_deletion_commits(self):
        callbacks = []
        with (
            mock.patch(
                "django.db.transaction.on_commit",
                side_effect=lambda func, using=None: callbacks.append(func),
            ),
            mock.patch(DISABLE_ROOM) as disable_room,
        ):
            self.discussion.delete()
            disable_room.assert_not_called()
            for callback in callbacks:
                callback()
        # Exported whatever MATRIX_HISTORY_EXPORT_ENABLED says: the record
        # keeps the room's history.
        disable_room.assert_called_once_with(
            str(self.room.uuid),
            reason="Step discussion deletion",
            export_history=True,
        )

    def test_one_deleted_while_matrix_was_off_is_closed_nightly(self):
        with override_config(**MATRIX_OFF), mock.patch(DISABLE_ROOM) as disable_room:
            self.discussion.delete()
        disable_room.assert_not_called()
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, matrix_models.RoomStates.ACTIVE)

        with mock.patch(DISABLE_ROOM) as disable_room:
            self.assertEqual(matrix_tasks.close_rooms_of_ended_scopes(), 1)
        disable_room.assert_called_once_with(
            str(self.room.uuid),
            reason="Step discussion deletion",
            export_history=True,
        )

    def test_the_nightly_task_resyncs_open_discussions(self):
        # Membership changed while Matrix chat was off was never synced.
        with mock.patch(SYNC_ROOM) as sync:
            matrix_tasks.resync_scope_rooms_nightly()
        sync.assert_called_once_with(str(self.room.uuid))


@override_config(**MATRIX)
class ReactivatedUserTest(DiscussionTestBase):
    def test_their_open_discussions_are_resynced(self):
        discussion = self.open_discussion()
        # Bob was deactivated before the room's first sync: no member row
        # finds the room for him.
        room = self.activate_room(discussion, self.alice)
        self.assertEqual(
            step_discussions.discussion_ids_for_user(self.bob), [discussion.pk]
        )
        self.assertEqual(step_discussions.discussion_ids_for_user(self.outsider), [])

        with (
            mock.patch.object(
                matrix_tasks.matrix_client,
                "is_homeserver_configured",
                return_value=True,
            ),
            mock.patch(SYNC_ROOM) as sync,
        ):
            matrix_tasks.restore_matrix_access(self.bob.uuid.hex)
        sync.assert_called_once_with(str(room.uuid))

    def test_closed_discussions_are_not_named(self):
        discussion = self.open_discussion()
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(discussion)
        self.assertEqual(step_discussions.discussion_ids_for_user(self.bob), [])


@override_config(**MATRIX)
class OpenWhileMatrixSwitchesOffTest(DiscussionTestBase):
    def test_a_room_refused_by_matrix_chat_keeps_no_record(self):
        with mock.patch(
            "waldur_mastermind.matrix_chat.room_provisioning.matrix_client"
        ) as provisioning_client:
            provisioning_client.is_enabled.return_value = False
            response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Matrix chat is disabled", str(response.data))
        self.assertFalse(models.StepDiscussion.objects.exists())


# =============================================================================
# Who is kept away, and who keeps standing
# =============================================================================


def export_url(export):
    return "http://testserver" + reverse(
        "matrix-export-detail", kwargs={"uuid": export.uuid.hex}
    )


@override_config(**MATRIX)
class RoundConflictTest(RoundDiscussionBase):
    """A round discussion keeps out, and keeps its record and export from,
    anyone on the applicant team of, author of, or in conflict with any
    proposal of the round on its panel, whatever that proposal's step
    status."""

    def setUp(self):
        super().setUp()
        # A call manager on the applicant team of the first proposal, on
        # panel A.
        self.insider = structure_factories.UserFactory()
        self.call.add_user(self.insider, CallRole.MANAGER)
        self.first.add_user(self.insider, ProposalRole.MEMBER)
        response = self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.discussion = models.StepDiscussion.objects.get(uuid=response.data["uuid"])

    def activate(self, discussion, *users):
        room = step_discussions.room_of(discussion)
        room.state = matrix_models.RoomStates.ACTIVE
        room.room_id = f"!{discussion.pk}:matrix.example.com"
        room.save(update_fields=["state", "room_id"])
        for user in users:
            matrix_models.MatrixRoomMember.objects.create(
                room=room,
                user=user,
                matrix_user_id=f"@{user.username}:matrix.example.com",
                membership_state=matrix_models.MembershipStates.JOINED,
            )
        return room

    def test_an_applicant_stays_out_whatever_their_proposals_step_status(self):
        instance = self.instance(self.first)
        for step_status in (
            WorkflowStepInstanceStatuses.ACTIVE,
            WorkflowStepInstanceStatuses.EXPIRED,
            WorkflowStepInstanceStatuses.WITHDRAWN,
            WorkflowStepInstanceStatuses.PENDING,
            WorkflowStepInstanceStatuses.COMPLETED,
        ):
            models.ProposalWorkflowStepInstance.objects.filter(pk=instance.pk).update(
                status=step_status
            )
            self.assertNotIn(
                self.insider.pk,
                step_discussions.participant_ids(self.discussion),
                step_status,
            )

    def test_an_applicant_stays_out_once_their_proposals_meeting_expired(self):
        room = self.activate(self.discussion, self.chair_a, self.manager)
        instance = self.instance(self.first)
        with mock.patch(DISABLE_ROOM), transaction.atomic():
            workflow_service.expire_step(instance)
        self.discussion.refresh_from_db()
        # The second proposal is still at the meeting.
        self.assertIsNone(self.discussion.closed_at)
        self.assertNotIn(
            self.insider.pk, step_discussions.participant_ids(self.discussion)
        )
        self.assertFalse(matrix_models.has_room_role(self.insider, room))

    def test_a_conflict_with_any_proposal_of_the_panel_keeps_them_out(self):
        # Chair A still writes the first proposal's report.
        profile = factories.ReviewerProfileFactory(user=self.chair_a)
        factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.second, status=COIStatuses.PENDING
        )
        self.assertNotIn(
            self.chair_a.pk, step_discussions.participant_ids(self.discussion)
        )

    def test_only_the_panels_proposals_count(self):
        response = self.open_discussion(self.chair_b, self.panel_b)
        discussion_b = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        self.assertIn(self.insider.pk, step_discussions.participant_ids(discussion_b))

    def test_the_record_and_its_export_are_kept_from_them(self):
        room = self.activate(self.discussion, self.chair_a, self.manager)
        self.client.force_authenticate(self.insider)
        self.assertEqual(
            self.client.get(detail_url(self.discussion)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(self.client.get(LIST_URL).data, [])

        with mock.patch(DISABLE_ROOM):
            step_discussions.close(self.discussion)
        export = matrix_models.MatrixHistoryExport.objects.create(
            room=room,
            export_type=matrix_models.ExportTypes.ON_DELETION,
            state=matrix_models.ExportStates.COMPLETED,
        )
        self.assertFalse(
            step_discussions.export_reader_ids(self.insider)
            .filter(id=self.discussion.pk)
            .exists()
        )
        self.assertEqual(
            self.client.get(export_url(export)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        # A manager without a conflict still reads both.
        self.client.force_authenticate(self.manager)
        response = self.client.get(detail_url(self.discussion))
        self.assertEqual(response.data["export_uuid"], export.uuid.hex)
        self.assertEqual(
            self.client.get(export_url(export)).status_code, status.HTTP_200_OK
        )

    def test_the_steps_show_them_no_discussion(self):
        self.assertIsNone(
            step_discussions.viewer_state(
                self.insider, proposal_step=self.instance(self.first)
            )
        )
        self.assertIsNone(
            step_discussions.viewer_state(
                self.insider, round_step=self.round_step, panel=self.panel_a
            )
        )
        self.assertIsNotNone(
            step_discussions.viewer_state(
                self.insider, round_step=self.round_step, panel=self.panel_b
            )
        )

    def test_someone_not_taking_part_learns_nothing_of_a_proposal_without_panel(self):
        models.Proposal.objects.filter(pk=self.third.pk).update(panel=None)
        instance = self.instance(self.third)
        response = self.open_discussion(
            self.outsider, proposal_step_uuid=instance.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.open_discussion(
            self.chair_b, proposal_step_uuid=instance.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("no panel", str(response.data))


@override_config(**MATRIX)
class StandingTest(DiscussionTestBase):
    """Who took part keeps no place by it: only standing held now counts."""

    def setUp(self):
        super().setUp()
        self.assessment.discussion_call_managers = False
        self.assessment.save()

    def test_a_revoked_holder_who_submitted_leaves(self):
        self.instance.contributions.filter(contributor=self.bob).update(
            status=StepContributionStatuses.SUBMITTED
        )
        assignment = models.ProposalRoleAssignment.objects.get(user=self.bob)
        proposal_roles.revoke(assignment, revoked_by=self.manager)
        self.instance.refresh_from_db()
        self.assertEqual(self.participants(), {self.alice.pk})

    def test_a_slot_handed_to_someone_who_may_not_take_it_brings_nobody(self):
        # The step lets nobody take slots over.
        self.instance.contributions.filter(contributor=self.alice).update(
            delegate=self.outsider
        )
        self.assertNotIn(self.outsider.pk, self.participants())

    def test_an_offering_role_change_resyncs_proposals_requesting_it(self):
        self.assessment.completer = ResponsibleRoles.OFFERING_MANAGER
        self.assessment.save()
        requested = factories.RequestedOfferingFactory(call=self.call)
        factories.RequestedResourceFactory(
            proposal=self.proposal, requested_offering=requested
        )
        other = factories.RequestedOfferingFactory(call=self.call)
        discussion = self.open_discussion()
        provider = structure_factories.UserFactory()

        with mock.patch(RESYNC) as resync:
            other.offering.add_user(provider, OfferingRole.MANAGER)
        resync.assert_not_called()

        with mock.patch(RESYNC) as resync:
            requested.offering.add_user(provider, OfferingRole.MANAGER)
        self.assertEqual([c.args[0].pk for c in resync.call_args_list], [discussion.pk])
        self.instance.refresh_from_db()
        self.assertIn(provider.pk, self.participants())


@override_config(**MATRIX)
class OpenerTest(DiscussionTestBase):
    def test_opening_it_gives_no_read_of_its_own(self):
        discussion = self.open_discussion(self.alice)
        self.activate_room(discussion, self.bob)
        self.client.force_authenticate(self.alice)
        # Not in the room until its sync takes her in.
        self.assertEqual(
            self.client.get(detail_url(discussion)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_the_opener_who_joins_the_team_reads_it_no_more(self):
        discussion = self.open_discussion(self.alice)
        self.activate_room(discussion, self.alice, self.bob)
        self.proposal.add_user(self.alice, ProposalRole.MEMBER)
        self.assertNotIn(self.alice.pk, step_discussions.participant_ids(discussion))
        self.client.force_authenticate(self.alice)
        self.assertEqual(
            self.client.get(detail_url(discussion)).status_code,
            status.HTTP_404_NOT_FOUND,
        )


@override_config(**MATRIX, PAT_ENABLED=True)
class TokenTest(DiscussionTestBase):
    """A personal access token reads a call's discussions as its manager
    only where its scopes allow CALL.UPDATE."""

    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()
        self.room = self.activate_room(self.discussion, self.alice)
        self.manager.can_use_personal_access_tokens = True
        self.manager.save()

    def use_token(self, *permissions):
        pat = _create_pat(
            self.manager, scopes=[p.value for p in permissions], bindings=()
        )
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))

    def test_the_list_needs_call_update(self):
        self.use_token(PermissionEnum.LIST_CALLS)
        self.assertEqual(self.client.get(LIST_URL).data, [])
        self.use_token(PermissionEnum.LIST_CALLS, PermissionEnum.UPDATE_CALL)
        self.assertEqual(
            [d["uuid"] for d in self.client.get(LIST_URL).data],
            [self.discussion.uuid.hex],
        )

    def test_the_export_needs_call_update(self):
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(self.discussion)
        export = matrix_models.MatrixHistoryExport.objects.create(
            room=self.room,
            export_type=matrix_models.ExportTypes.ON_DELETION,
            state=matrix_models.ExportStates.COMPLETED,
        )
        self.use_token(PermissionEnum.CREATE_MATRIX_ROOM)
        self.assertEqual(
            self.client.get(export_url(export)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.use_token(PermissionEnum.CREATE_MATRIX_ROOM, PermissionEnum.UPDATE_CALL)
        self.assertEqual(
            self.client.get(export_url(export)).status_code, status.HTTP_200_OK
        )


@override_config(**MATRIX)
class OpenRefusalTest(DiscussionTestBase):
    """Whoever does not take part is refused alike, whatever the step's
    state, so the answer says nothing about it."""

    def assert_refused_alike(self):
        for user in (self.outsider, self.applicant):
            response = self.open(user)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(
                str(response.data["detail"]), step_discussions.NOT_TAKING_PART
            )

    def test_while_the_step_is_active(self):
        self.assert_refused_alike()

    def test_once_closed(self):
        discussion = self.open_discussion()
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(discussion)
        models.ProposalWorkflowStepInstance.objects.filter(pk=self.instance.pk).update(
            status=WorkflowStepInstanceStatuses.COMPLETED
        )
        self.assert_refused_alike()
        self.assertEqual(self.open(self.alice).status_code, status.HTTP_409_CONFLICT)

    def test_while_the_step_is_not_active(self):
        models.ProposalWorkflowStepInstance.objects.filter(pk=self.instance.pk).update(
            status=WorkflowStepInstanceStatuses.PENDING
        )
        self.assert_refused_alike()
        self.assertEqual(self.open(self.alice).status_code, status.HTTP_409_CONFLICT)

    def test_on_a_step_without_discussion(self):
        self.assessment.has_discussion = False
        self.assessment.save()
        self.assert_refused_alike()
        self.assertEqual(self.open(self.alice).status_code, status.HTTP_400_BAD_REQUEST)


@override_config(**MATRIX)
class SwitchedOffTest(DiscussionTestBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()
        self.room = self.activate_room(self.discussion, self.alice, self.bob)

    def patch_step(self, data):
        self.client.force_authenticate(self.manager)
        with mock.patch(DISABLE_ROOM) as disable_room:
            response = self.client.patch(
                self.step_url(self.assessment), data, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return disable_room

    def test_its_open_discussions_close_their_history_exported(self):
        disable_room = self.patch_step({"has_discussion": False})
        self.discussion.refresh_from_db()
        self.assertIsNotNone(self.discussion.closed_at)
        disable_room.assert_called_once_with(
            str(self.room.uuid),
            reason=step_discussions.ROOM_CLOSING_REASON,
            export_history=True,
        )
        # The record opens nothing, and the step shows none.
        self.assertIsNone(
            step_discussions.viewer_state(self.alice, proposal_step=self.instance)
        )
        self.assertEqual(self.open(self.alice).status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_step_turning_blind_keeps_no_room(self):
        self.patch_step({"has_discussion": False, "blind_review": True})
        self.discussion.refresh_from_db()
        self.assertIsNotNone(self.discussion.closed_at)

    def test_a_rename_closes_nothing(self):
        disable_room = self.patch_step({"name": "Renamed"})
        disable_room.assert_not_called()
        self.discussion.refresh_from_db()
        self.assertIsNone(self.discussion.closed_at)


# =============================================================================
# A step running again, and a closing export that failed
# =============================================================================


@override_config(**MATRIX)
class ReactivatedStepTest(DiscussionTestBase):
    def close_and_reactivate(self):
        discussion = self.open_discussion()
        self.activate_room(discussion, self.alice, self.bob)
        with mock.patch(DISABLE_ROOM):
            self.complete_step()
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)
        with transaction.atomic():
            workflow_service._reactivate(self.proposal, self.instance)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.ACTIVE)
        return discussion

    def test_it_opens_a_new_discussion(self):
        closed = self.close_and_reactivate()
        state = step_discussions.viewer_state(self.alice, proposal_step=self.instance)
        self.assertEqual(
            state, {"uuid": closed.uuid.hex, "state": "closed", "can_open": True}
        )

        response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(response.data["uuid"], closed.uuid.hex)
        self.assertEqual(models.StepDiscussion.objects.count(), 2)
        reopened = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        self.assertNotEqual(
            step_discussions.room_of(reopened), step_discussions.room_of(closed)
        )
        # Later opens return the new one; the step shows it.
        again = self.open(self.bob)
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.data["uuid"], reopened.uuid.hex)
        state = step_discussions.viewer_state(self.alice, proposal_step=self.instance)
        self.assertEqual(state["uuid"], reopened.uuid.hex)
        self.assertEqual(state["state"], "open")

    def test_the_closed_one_stays_readable(self):
        closed = self.close_and_reactivate()
        self.open(self.alice)
        self.client.force_authenticate(self.manager)
        response = self.client.get(LIST_URL, {"proposal_uuid": self.proposal.uuid.hex})
        self.assertEqual(len(response.data), 2)
        self.assertIn(closed.uuid.hex, [d["uuid"] for d in response.data])

    def test_two_open_ones_are_refused_by_the_database(self):
        self.open_discussion()
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.StepDiscussion.objects.create(proposal_step=self.instance)

    def test_a_rejected_proposal_opens_nothing(self):
        discussion = self.open_discussion()
        models.Proposal.objects.filter(pk=self.proposal.pk).update(
            state=ProposalStates.REJECTED
        )
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(discussion)
        response = self.open(self.alice)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


@override_config(**MATRIX)
class RoundRunningAgainTest(RoundDiscussionBase):
    def test_a_round_discussion_closed_while_its_round_step_runs_opens_anew(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        closed = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(closed)
        response = self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(response.data["uuid"], closed.uuid.hex)


@override_config(**MATRIX)
class FailedClosingExportTest(DiscussionTestBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()
        self.room = self.activate_room(self.discussion, self.alice, self.bob)
        with mock.patch(DISABLE_ROOM):
            self.complete_step()
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, matrix_models.RoomStates.DISABLING)

    def disable(self, export_error=None):
        with (
            mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client") as client,
            mock.patch("waldur_mastermind.matrix_chat.tasks.post_as_bot"),
            mock.patch("waldur_mastermind.matrix_chat.tasks._end_room_call"),
        ):
            client.is_enabled.return_value = True

            def export_before_closing(room):
                # The members are still in the room while it is exported.
                self.assertFalse(client.kick_user.called)
                if export_error is not None:
                    raise export_error

            with mock.patch(
                "waldur_mastermind.matrix_chat.tasks._export_before_closing",
                side_effect=export_before_closing,
            ):
                matrix_tasks.disable_room(str(self.room.uuid), export_history=True)
        self.room.refresh_from_db()
        return client

    def test_the_room_stays_open_with_its_members(self):
        client = self.disable(matrix_tasks.HistoryNotExported("bot is down"))
        client.kick_user.assert_not_called()
        self.assertEqual(self.room.state, matrix_models.RoomStates.ACTIVE)
        self.assertIn("bot is down", self.room.error_message)
        self.assertEqual(
            self.room.members.filter(
                membership_state=matrix_models.MembershipStates.JOINED
            ).count(),
            2,
        )

    def test_the_nightly_export_closes_it_again(self):
        self.disable(matrix_tasks.HistoryNotExported("bot is down"))
        with mock.patch(DISABLE_ROOM) as disable_room:
            self.assertEqual(matrix_tasks.close_rooms_of_ended_scopes(), 1)
        disable_room.assert_called_once_with(
            str(self.room.uuid),
            reason=step_discussions.ROOM_CLOSING_REASON,
            export_history=True,
        )

    def test_exported_first_then_closed(self):
        client = self.disable()
        self.assertEqual(client.kick_user.call_count, 2)
        self.assertEqual(self.room.state, matrix_models.RoomStates.ARCHIVED)


# =============================================================================
# The nightly backstop
# =============================================================================


@override_config(**MATRIX)
class CloseStaleTest(RoundDiscussionBase):
    """Discussions whose step ended without the events that close them: the
    proposals' steps moved on in bulk, as no signal tells."""

    def instances(self, *proposals):
        return models.ProposalWorkflowStepInstance.objects.filter(
            proposal__in=proposals, call_step=self.meeting, run__isnull=True
        )

    def test_closes_those_whose_step_ended_and_only_those(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        discussion_a = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        response = self.open_discussion(self.chair_b, self.panel_b)
        discussion_b = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        self.assertEqual(list(step_discussions.stale_ids()), [])

        self.instances(self.first, self.second).update(
            status=WorkflowStepInstanceStatuses.COMPLETED
        )
        with CaptureQueriesContext(connection) as context:
            stale = list(step_discussions.stale_ids())
        self.assertEqual(len(context.captured_queries), 1)
        self.assertEqual(stale, [discussion_a.pk])

        with mock.patch(DISABLE_ROOM):
            self.assertEqual(step_discussions.close_stale(), 1)
            self.assertEqual(step_discussions.close_stale(), 0)
        discussion_a.refresh_from_db()
        discussion_b.refresh_from_db()
        self.assertIsNotNone(discussion_a.closed_at)
        self.assertIn(
            self.chair_a.pk, discussion_a.participants.values_list("id", flat=True)
        )
        self.assertIsNone(discussion_b.closed_at)

    def test_a_proposal_still_to_reach_the_step_keeps_it_open(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        discussion = models.StepDiscussion.objects.get(uuid=response.data["uuid"])
        self.instances(self.first).update(status=WorkflowStepInstanceStatuses.COMPLETED)
        self.instances(self.second).update(status=WorkflowStepInstanceStatuses.PENDING)
        self.assertNotIn(discussion.pk, list(step_discussions.stale_ids()))


@override_config(**MATRIX)
class CloseStaleProposalStepTest(DiscussionTestBase):
    def test_a_step_no_longer_active_or_a_proposal_withdrawn(self):
        discussion = self.open_discussion()
        self.assertEqual(list(step_discussions.stale_ids()), [])
        models.Proposal.objects.filter(pk=self.proposal.pk).update(
            state=ProposalStates.CANCELED
        )
        self.assertEqual(list(step_discussions.stale_ids()), [discussion.pk])
        models.Proposal.objects.filter(pk=self.proposal.pk).update(
            state=ProposalStates.IN_REVIEW
        )
        models.ProposalWorkflowStepInstance.objects.filter(pk=self.instance.pk).update(
            status=WorkflowStepInstanceStatuses.COMPLETED
        )
        self.assertEqual(list(step_discussions.stale_ids()), [discussion.pk])
        self.assertEqual(step_discussions.close_stale(), 1)
        discussion.refresh_from_db()
        self.assertIsNotNone(discussion.closed_at)


# =============================================================================
# What it costs, and what resyncs
# =============================================================================


@override_config(**MATRIX)
class RoundQueryCountTest(RoundDiscussionBase):
    """Panel A has five proposals at the meeting, panel B one."""

    def open_all(self):
        self.extra = [self.make_proposal(self.chair_a) for _ in range(3)]
        models.Proposal.objects.filter(pk__in=[p.pk for p in self.extra]).update(
            panel=self.panel_a
        )
        for proposal in self.extra:
            self.pass_check(proposal)
        return super().open_all()

    def queries(self, func):
        with CaptureQueriesContext(connection) as context:
            func()
        return len(context.captured_queries)

    def test_the_same_queries_for_one_proposal_as_for_five(self):
        covered = {
            panel: step_discussions.round_instances(self.round_step, panel).count()
            for panel in (self.panel_a, self.panel_b)
        }
        self.assertEqual(covered, {self.panel_a: 5, self.panel_b: 1})
        # Loaded as the views load it.
        round_step = models.RoundStep.objects.select_related(
            "call_step", "round__call"
        ).get(pk=self.round_step.pk)

        def participants(panel):
            return self.queries(
                lambda: step_discussions.people(
                    models.StepDiscussion(round_step=round_step, panel=panel)
                )
            )

        def viewer(panel):
            return self.queries(
                lambda: step_discussions.viewer_state(
                    self.manager, round_step=round_step, panel=panel
                )
            )

        self.assertEqual(participants(self.panel_b), participants(self.panel_a))
        self.assertEqual(viewer(self.panel_b), viewer(self.panel_a))

    def test_the_same_queries_once_opened_with_what_they_covered(self):
        for panel in (self.panel_a, self.panel_b):
            response = self.open_discussion(self.manager, panel)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        round_step = models.RoundStep.objects.select_related(
            "call_step", "round__call"
        ).get(pk=self.round_step.pk)

        def counts(panel):
            discussion = models.StepDiscussion.objects.select_related(
                "round_step__call_step", "round_step__round__call", "panel"
            ).get(round_step=round_step, panel=panel)
            self.assertEqual(
                discussion.covered_proposals.count(),
                5 if panel == self.panel_a else 1,
            )
            return (
                self.queries(lambda: step_discussions.people(discussion)),
                self.queries(
                    lambda: step_discussions.viewer_state(
                        self.manager, round_step=round_step, panel=panel
                    )
                ),
            )

        self.assertEqual(counts(self.panel_b), counts(self.panel_a))

    def test_the_round_lists_its_panels_once_each(self):
        self.client.force_authenticate(self.manager)
        url = factories.RoundFactory.get_url(self.call, self.round)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Each panel's people are worked out once per response, however many
        # proposals it has.
        with mock.patch.object(
            step_discussions, "_round_people", wraps=step_discussions._round_people
        ) as round_people:
            self.client.get(url)
        targets = [(c.args[0].pk, c.args[2]) for c in round_people.call_args_list]
        self.assertEqual(len(targets), len(set(targets)))


def query_count(client, user, url):
    """Queries of a GET of ``url`` as ``user``, once its caches are warm."""
    client.force_authenticate(user)
    client.get(url)
    with CaptureQueriesContext(connection) as context:
        response = client.get(url)
    assert response.status_code == status.HTTP_200_OK, response.data
    return len(context.captured_queries)


@override_config(**MATRIX)
class RoundDetailCostTest(RoundDiscussionBase):
    """What the rows of a round's steps ask alike is asked once: the
    discussions of its panels cost no more queries for more panels."""

    def counts(self):
        urls = (
            factories.RoundFactory.get_url(self.call, self.round),
            factories.RoundFactory.get_list_url(self.call),
        )
        return {
            (user.pk, url): query_count(self.client, user, url)
            for user in (self.manager, self.chair_a)
            for url in urls
        }

    def test_more_panels_cost_no_more_queries(self):
        response = self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        before = self.counts()
        for name in ("Panel C", "Panel D"):
            panel = models.CallPanel.objects.create(call=self.call, name=name)
            panel.members.add(self.chair_a)
        after = self.counts()
        for key, count in before.items():
            self.assertLessEqual(after[key], count, key)


@override_config(**MATRIX)
class StartableCostTest(DiscussionTestBase):
    def add_proposal(self):
        proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        for user in (self.alice, self.bob):
            models.ProposalRoleAssignment.objects.create(
                proposal=proposal, role=self.role, user=user
            )
        with transaction.atomic():
            workflow_service.create_step_instances(proposal)
            workflow_service.activate_first_step(proposal)

    def test_a_call_manager_pays_nothing_per_candidate(self):
        before = (
            query_count(self.client, self.manager, STARTABLE_URL),
            query_count(self.client, self.alice, STARTABLE_URL),
        )
        for _ in range(4):
            self.add_proposal()
        self.assertEqual(len(startable(self.client, self.manager)), 5)
        self.assertEqual(len(startable(self.client, self.alice)), 5)
        self.assertLessEqual(
            query_count(self.client, self.manager, STARTABLE_URL), before[0]
        )
        # Who works on a step is still asked per step, for someone who does
        # not oversee the call; at most this many queries each.
        self.assertLessEqual(
            query_count(self.client, self.alice, STARTABLE_URL) - before[1], 4 * 7
        )

    def test_the_steps_of_a_proposal_cost_its_overseer_nothing_each(self):
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        before = query_count(self.client, self.manager, url)
        for order in (2, 3):
            step = self.add_step(
                f"review{order}",
                StepKinds.ASSESSMENT,
                order,
                outcome_set=OutcomeSets.DONE,
                completer=ResponsibleRoles.CALL_MANAGER,
                has_discussion=True,
            )
            models.ProposalWorkflowStepInstance.objects.create(
                proposal=self.proposal, call_step=step, order=step.order
            )
        self.assertLessEqual(query_count(self.client, self.manager, url), before)


@override_config(**MATRIX)
class NarrowResyncTest(DiscussionTestBase):
    """Only the discussions a change can affect are resynced, each once."""

    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()

    def resynced(self, action):
        with mock.patch(RESYNC) as resync:
            action()
        return [call.args[0].pk for call in resync.call_args_list]

    def test_a_call_manager_on_a_step_without_observers(self):
        self.assessment.discussion_call_managers = False
        self.assessment.save()
        user = structure_factories.UserFactory()
        self.assertEqual(
            self.resynced(lambda: self.call.add_user(user, CallRole.MANAGER)), []
        )
        # One in the room is resynced: who moderates may change.
        self.activate_room(self.discussion, self.manager)
        self.assertEqual(
            self.resynced(
                lambda: self.call.remove_user(self.manager, CallRole.MANAGER)
            ),
            [self.discussion.pk],
        )

    def test_a_reviewer_on_a_step_taking_none(self):
        user = structure_factories.UserFactory()
        self.assertEqual(
            self.resynced(lambda: self.call.add_user(user, CallRole.REVIEWER)), []
        )

    def test_a_panel_renamed_or_its_chair_changed(self):
        panel = models.CallPanel.objects.create(call=self.call, name="Physics")
        models.Proposal.objects.filter(pk=self.proposal.pk).update(panel=panel)
        panel.name = "Physics and Astronomy"
        self.assertEqual(self.resynced(panel.save), [])
        panel.chair = self.outsider
        self.assertEqual(self.resynced(panel.save), [self.discussion.pk])

    def test_a_panel_the_proposal_is_not_on(self):
        self.assessment.discussion_panel_members = True
        self.assessment.save()
        panel = models.CallPanel.objects.create(call=self.call, name="Physics")
        self.assertEqual(self.resynced(lambda: panel.members.add(self.outsider)), [])

    def test_the_steps_observer_settings(self):
        self.assessment.discussion_call_managers = False
        self.assertEqual(self.resynced(self.assessment.save), [self.discussion.pk])
        self.assessment.name = "Renamed"
        self.assertEqual(self.resynced(self.assessment.save), [])

    def test_the_calls_panel_chair(self):
        # The proposal has no panel: the call's chair moderates.
        self.call.panel_chair = self.outsider
        self.assertEqual(self.resynced(self.call.save), [self.discussion.pk])
        self.call.name = "Renamed"
        self.assertEqual(self.resynced(self.call.save), [])

    def test_once_per_discussion_after_commit(self):
        callbacks = []
        user = structure_factories.UserFactory()
        with (
            mock.patch(
                "django.db.transaction.on_commit",
                side_effect=lambda func, using=None: callbacks.append(func),
            ),
            mock.patch(RESYNC) as resync,
        ):
            with transaction.atomic():
                self.call.add_user(user, CallRole.MANAGER)
                self.assessment.discussion_panel_members = True
                self.assessment.save()
                contribution = self.instance.contributions.get(contributor=self.alice)
                contribution.save()
            resync.assert_not_called()
            for callback in callbacks:
                callback()
        self.assertEqual(
            [call.args[0].pk for call in resync.call_args_list], [self.discussion.pk]
        )


# =============================================================================
# Second review: moved proposals, support, the round open path, closed
# rooms still active, tokens, and step changes
# =============================================================================


def make_export(room):
    return matrix_models.MatrixHistoryExport.objects.create(
        room=room,
        export_type=matrix_models.ExportTypes.ON_DELETION,
        state=matrix_models.ExportStates.COMPLETED,
    )


class SecondReviewRoundBase(RoundDiscussionBase):
    def open_panel(self, user, panel):
        response = self.open_discussion(user, panel)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return models.StepDiscussion.objects.get(uuid=response.data["uuid"])

    def activate(self, discussion, *users):
        return DiscussionTestBase.activate_room(self, discussion, *users)

    def move(self, proposal, panel):
        proposal.refresh_from_db()
        proposal.panel = panel
        with mock.patch(RESYNC):
            proposal.save()


@override_config(**MATRIX)
class MovedProposalTest(SecondReviewRoundBase):
    """A proposal moved to another panel keeps its team out of the room it
    was discussed in, and out of its record and export."""

    def setUp(self):
        super().setUp()
        self.meeting.discussion_panel_members = True
        self.meeting.save()
        # A panel A member on the first proposal's team, and a call manager.
        self.insider = self.panel_member()
        self.panel_a.members.add(self.insider)
        self.first.add_user(self.insider, ProposalRole.MEMBER)
        self.manager_insider = structure_factories.UserFactory()
        self.call.add_user(self.manager_insider, CallRole.MANAGER)
        self.first.add_user(self.manager_insider, ProposalRole.MEMBER)
        self.discussion = self.open_panel(self.chair_a, self.panel_a)

    def test_its_team_stays_out_of_the_old_panels_room(self):
        room = self.activate(self.discussion, self.chair_a)
        self.move(self.first, self.panel_b)
        self.assertNotIn(
            self.insider.pk, step_discussions.participant_ids(self.discussion)
        )
        self.assertFalse(matrix_models.has_room_role(self.insider, room))
        self.assertIn(self.first, step_discussions.covered_proposals(self.discussion))

    def test_its_team_stays_out_of_the_old_panels_export(self):
        room = self.activate(self.discussion, self.chair_a, self.manager)
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(self.discussion)
        export = make_export(room)
        self.move(self.first, self.panel_b)
        self.client.force_authenticate(self.manager_insider)
        self.assertEqual(
            self.client.get(export_url(export)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(detail_url(self.discussion)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_moving_it_back_admits_nobody(self):
        # A panel B member on the first proposal's team sits in panel B's
        # room until the proposal comes to panel B.
        b_insider = self.panel_member()
        self.panel_b.members.add(b_insider)
        self.first.add_user(b_insider, ProposalRole.MEMBER)
        discussion_b = self.open_panel(self.chair_b, self.panel_b)
        self.assertIn(b_insider.pk, step_discussions.participant_ids(discussion_b))

        self.move(self.first, self.panel_b)
        self.assertNotIn(b_insider.pk, step_discussions.participant_ids(discussion_b))
        self.move(self.first, self.panel_a)
        self.assertNotIn(b_insider.pk, step_discussions.participant_ids(discussion_b))
        self.assertNotIn(
            self.insider.pk, step_discussions.participant_ids(self.discussion)
        )

    def test_a_panel_member_applying_elsewhere_keeps_their_own_room(self):
        # The third proposal is on panel B; its team member on panel A
        # still sits in panel A's room.
        member = self.panel_member()
        self.panel_a.members.add(member)
        self.third.add_user(member, ProposalRole.MEMBER)
        self.assertIn(member.pk, step_discussions.participant_ids(self.discussion))


@override_config(**MATRIX)
class SupportOnTheTeamTest(SecondReviewRoundBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_panel(self.chair_a, self.panel_a)
        self.room = self.activate(self.discussion, self.chair_a)
        self.support = structure_factories.UserFactory(is_support=True)
        self.first.add_user(self.support, ProposalRole.MEMBER)
        self.other_support = structure_factories.UserFactory(is_support=True)

    def join(self, user):
        self.client.force_authenticate(user)
        url = (
            "http://testserver"
            + reverse("matrix-room-detail", kwargs={"uuid": self.room.uuid.hex})
            + "join/"
        )
        with mock.patch(
            "waldur_mastermind.matrix_chat.tasks.staff_join_room.delay"
        ) as staff_join:
            response = self.client.post(url)
        return response, staff_join

    def test_they_read_no_record_nor_export(self):
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(self.discussion)
        export = make_export(self.room)
        self.client.force_authenticate(self.support)
        self.assertEqual(
            self.client.get(detail_url(self.discussion)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(export_url(export)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        # Other support still read both.
        self.client.force_authenticate(self.other_support)
        self.assertEqual(
            self.client.get(export_url(export)).status_code, status.HTTP_200_OK
        )

    def test_they_cannot_join_its_room(self):
        # The room is not theirs to see, so not to join either.
        response, staff_join = self.join(self.support)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        staff_join.assert_not_called()
        response, staff_join = self.join(self.other_support)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        staff_join.assert_called_once()

    def room_url(self, action=""):
        path = reverse("matrix-room-detail", kwargs={"uuid": self.room.uuid.hex})
        return "http://testserver" + path + (f"{action}/" if action else "")

    def test_they_see_neither_the_room_nor_its_members_nor_export_it(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.first.add_user(staff, ProposalRole.MEMBER)
        list_url = "http://testserver" + reverse("matrix-room-list")
        for user, visible in (
            (self.support, False),
            (self.other_support, True),
            # Staff see every discussion, as they read every record.
            (staff, True),
        ):
            self.client.force_authenticate(user)
            listed = [r["uuid"] for r in self.client.get(list_url).data]
            self.assertEqual(self.room.uuid.hex in listed, visible, user)
            expected = status.HTTP_200_OK if visible else status.HTTP_404_NOT_FOUND
            self.assertEqual(self.client.get(self.room_url()).status_code, expected)
            self.assertEqual(
                self.client.get(self.room_url("members")).status_code, expected
            )
            with mock.patch(
                "waldur_mastermind.matrix_chat.tasks.export_room_history.delay"
            ):
                response = self.client.post(self.room_url("export_history"))
            # 200 for the export another asked for already.
            self.assertIn(
                response.status_code,
                (status.HTTP_200_OK, status.HTTP_202_ACCEPTED)
                if visible
                else (status.HTTP_404_NOT_FOUND,),
            )

    def sync(self):
        with mock.patch(
            "waldur_mastermind.matrix_chat.tasks.matrix_client"
        ) as matrix_client:
            matrix_client.is_enabled.return_value = True
            matrix_client.ensure_user_exists.side_effect = (
                lambda user: f"@{user.username}:matrix.example.com"
            )
            matrix_client.get_power_level_for_scope.return_value = 0
            matrix_tasks.sync_room_members(str(self.room.uuid))
        return matrix_client

    def join_with_join(self, user):
        matrix_models.MatrixRoomMember.objects.create(
            room=self.room,
            user=user,
            matrix_user_id=f"@{user.username}:matrix.example.com",
            membership_state=matrix_models.MembershipStates.JOINED,
            power_level=50,
            manually_joined=True,
        )

    def keep_away(self, user):
        with mock.patch(RESYNC):
            self.second.add_user(user, ProposalRole.MEMBER)

    def test_one_who_joined_leaves_once_kept_away(self):
        self.join_with_join(self.other_support)
        self.sync().kick_user.assert_not_called()
        self.assertTrue(matrix_models.keeps_room_access(self.other_support, self.room))

        self.keep_away(self.other_support)
        self.assertFalse(matrix_models.keeps_room_access(self.other_support, self.room))
        self.sync().kick_user.assert_called_once_with(
            self.room.room_id,
            f"@{self.other_support.username}:matrix.example.com",
            reason="Role removed in Waldur",
        )

    def test_one_kept_away_before_a_sync_took_no_part(self):
        self.join_with_join(self.other_support)
        self.keep_away(self.other_support)
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(self.discussion)
        took_part = set(self.discussion.participants.values_list("id", flat=True))
        self.assertNotIn(self.other_support.pk, took_part)
        self.assertIn(self.chair_a.pk, took_part)


@override_config(**MATRIX)
class RoundOpenRefusalTest(SecondReviewRoundBase):
    def test_a_stranger_gets_one_answer_whatever_the_panel(self):
        stranger = structure_factories.UserFactory()
        foreign = models.CallPanel.objects.create(
            call=factories.CallFactory(), name="Elsewhere"
        )
        for panel in (None, self.panel_a, foreign):
            response = self.open_discussion(stranger, panel)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(
                str(response.data["detail"]), step_discussions.NOT_TAKING_PART
            )

    def test_a_participant_is_told_to_name_the_panel(self):
        response = self.open_discussion(self.chair_a, None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.open_discussion(
            self.chair_a,
            models.CallPanel.objects.create(
                call=factories.CallFactory(), name="Elsewhere"
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_config(**MATRIX)
class ClosedRoomStillActiveTest(DiscussionTestBase):
    def test_a_conflict_after_closing_takes_them_out(self):
        discussion = self.open_discussion()
        room = self.activate_room(discussion, self.alice, self.bob)
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(discussion)
        # Its export failed: active again, its members kept.
        matrix_models.MatrixRoom.objects.filter(pk=room.pk).update(
            state=matrix_models.RoomStates.ACTIVE
        )
        self.proposal.add_user(self.bob, ProposalRole.MEMBER)
        room.refresh_from_db()
        self.assertFalse(matrix_models.has_room_role(self.bob, room))
        self.assertTrue(matrix_models.has_room_role(self.alice, room))


@override_config(**MATRIX, PAT_ENABLED=True)
class ParticipantTokenTest(DiscussionTestBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()
        self.activate_room(self.discussion, self.alice)
        self.alice.can_use_personal_access_tokens = True
        self.alice.save()

    def listed(self, *permissions, bindings=()):
        pat = _create_pat(
            self.alice, scopes=[p.value for p in permissions], bindings=bindings
        )
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [d["uuid"] for d in response.data]

    def open_with(self, *permissions, bindings=()):
        pat = _create_pat(
            self.alice, scopes=[p.value for p in permissions], bindings=bindings
        )
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        with mock.patch(CREATE_ROOM), mock.patch(SYNC_ROOM):
            return self.client.post(
                OPEN_URL, {"proposal_step_uuid": self.instance.uuid.hex}, format="json"
            )

    def test_opens_only_what_it_could_read(self):
        elsewhere = structure_factories.CustomerFactory()
        for permissions, bindings, expected in (
            ((PermissionEnum.LIST_CALLS,), (), status.HTTP_403_FORBIDDEN),
            (
                (PermissionEnum.LIST_PROPOSALS,),
                (elsewhere,),
                status.HTTP_403_FORBIDDEN,
            ),
            ((PermissionEnum.LIST_PROPOSALS,), (self.proposal,), status.HTTP_200_OK),
            ((PermissionEnum.LIST_PROPOSALS,), (), status.HTTP_200_OK),
        ):
            response = self.open_with(*permissions, bindings=bindings)
            self.assertEqual(response.status_code, expected, (permissions, bindings))

    def test_needs_proposal_list(self):
        self.assertEqual(self.listed(PermissionEnum.LIST_CALLS), [])
        self.assertEqual(
            self.listed(PermissionEnum.LIST_PROPOSALS), [self.discussion.uuid.hex]
        )

    def test_held_to_its_bindings(self):
        elsewhere = structure_factories.CustomerFactory()
        self.assertEqual(
            self.listed(PermissionEnum.LIST_PROPOSALS, bindings=[elsewhere]), []
        )
        self.assertEqual(
            self.listed(PermissionEnum.LIST_PROPOSALS, bindings=[self.proposal]),
            [self.discussion.uuid.hex],
        )
        self.assertEqual(
            self.listed(PermissionEnum.LIST_PROPOSALS, bindings=[self.call]),
            [self.discussion.uuid.hex],
        )


@override_config(**MATRIX)
class StepAndOfferingChangeTest(DiscussionTestBase):
    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()

    def resynced(self, action):
        with mock.patch(RESYNC) as resync:
            action()
        return [call.args[0].pk for call in resync.call_args_list]

    def test_who_completes_the_step(self):
        self.assessment.completer = ResponsibleRoles.CALL_MANAGER
        self.assertEqual(self.resynced(self.assessment.save), [self.discussion.pk])

    def test_offerings_requested(self):
        self.assessment.completer = ResponsibleRoles.OFFERING_MANAGER
        self.assessment.save()
        requested = factories.RequestedOfferingFactory(call=self.call)
        self.assertEqual(
            self.resynced(
                lambda: factories.RequestedResourceFactory(
                    proposal=self.proposal, requested_offering=requested
                )
            ),
            [self.discussion.pk],
        )
        requested.state = RequestedOfferingStates.CANCELED
        self.assertEqual(self.resynced(requested.save), [self.discussion.pk])


# =============================================================================
# Discussions the user could start
# =============================================================================

STARTABLE_URL = LIST_URL + "startable/"


def startable(client, user):
    client.force_authenticate(user)
    response = client.get(STARTABLE_URL)
    assert response.status_code == status.HTTP_200_OK, response.data
    return response.data


@override_config(**MATRIX)
class StartableTest(DiscussionTestBase):
    def test_lists_the_active_steps_its_participants_work_on(self):
        for user in (self.alice, self.bob):
            rows = startable(self.client, user)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["proposal_step_uuid"], self.instance.uuid.hex)
            self.assertIsNone(row["round_step_uuid"])
            self.assertEqual(row["proposal_uuid"], self.proposal.uuid.hex)
            self.assertEqual(row["step_name"], "Assessment")
            self.assertEqual(row["call_uuid"], self.call.uuid.hex)
        for user in (self.outsider, self.applicant):
            self.assertEqual(startable(self.client, user), [])

    def test_a_started_discussion_is_no_longer_listed(self):
        self.open_discussion(self.alice)
        self.assertEqual(startable(self.client, self.bob), [])

    def test_what_it_lists_opens(self):
        row = startable(self.client, self.bob)[0]
        response = self.open(self.bob, proposal_step_uuid=row["proposal_step_uuid"])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_conflict_on_record_keeps_it_off_the_list(self):
        profile = factories.ReviewerProfileFactory(user=self.bob)
        factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.proposal, status=COIStatuses.PENDING
        )
        self.assertEqual(startable(self.client, self.bob), [])
        self.assertEqual(len(startable(self.client, self.alice)), 1)

    def test_a_step_without_a_discussion_is_not_listed(self):
        self.assessment.has_discussion = False
        self.assessment.save(update_fields=["has_discussion"])
        self.assertEqual(startable(self.client, self.alice), [])

    def test_nothing_while_matrix_chat_is_disabled(self):
        with override_config(**MATRIX_OFF):
            self.assertEqual(startable(self.client, self.alice), [])


@override_config(**MATRIX)
class RoundStartableTest(RoundDiscussionBase):
    def rows(self, user):
        return [
            (row["round_step_uuid"], row["panel_uuid"])
            for row in startable(self.client, user)
        ]

    def test_one_row_per_panel_the_user_takes_part_in(self):
        round_step = self.round_step.uuid.hex
        self.assertEqual(self.rows(self.chair_a), [(round_step, self.panel_a.uuid.hex)])
        self.assertEqual(self.rows(self.chair_b), [(round_step, self.panel_b.uuid.hex)])
        self.assertEqual(
            sorted(self.rows(self.manager)),
            sorted(
                [
                    (round_step, self.panel_a.uuid.hex),
                    (round_step, self.panel_b.uuid.hex),
                ]
            ),
        )
        self.assertEqual(self.rows(self.outsider), [])

    def test_a_started_panel_discussion_drops_off(self):
        self.open_discussion(self.chair_a, self.panel_a)
        self.assertEqual(self.rows(self.chair_a), [])
        self.assertEqual(
            self.rows(self.manager), [(self.round_step.uuid.hex, self.panel_b.uuid.hex)]
        )

    def test_a_conflict_with_a_proposal_of_the_panel_drops_its_row(self):
        profile = factories.ReviewerProfileFactory(user=self.chair_a)
        factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.second, status=COIStatuses.PENDING
        )
        self.assertEqual(self.rows(self.chair_a), [])


@override_config(**MATRIX)
class DiscussionRoomKindTest(DiscussionTestBase):
    def test_the_room_says_it_is_a_discussion(self):
        discussion = self.open_discussion()
        self.activate_room(discussion, self.alice)
        self.client.force_authenticate(self.alice)
        url = "http://testserver" + reverse("matrix-room-list")
        data = self.client.get(url, {"member": True}).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["scope_type"], "step_discussion")
        self.assertEqual(
            data[0]["scope_subtitle"],
            f"Assessment · {self.proposal.slug or self.proposal.name}",
        )
        for kind, expected in (("step_discussion", 1), ("project", 0)):
            listed = self.client.get(url, {"member": True, "scope_type": kind}).data
            self.assertEqual(len(listed), expected)


@override_config(**MATRIX)
class DiscussionRoomListCostTest(DiscussionTestBase):
    add_proposal = StartableCostTest.add_proposal

    def test_their_names_cost_no_query_per_room(self):
        url = "http://testserver" + reverse("matrix-room-list")
        self.activate_room(self.open_discussion(), self.alice)
        one = query_count(self.client, self.alice, url)
        for _ in range(2):
            self.add_proposal()
        for instance in models.ProposalWorkflowStepInstance.objects.filter(
            call_step=self.assessment,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            discussions__isnull=True,
        ):
            response = self.open(self.alice, proposal_step_uuid=instance.uuid.hex)
            self.activate_room(
                models.StepDiscussion.objects.get(uuid=response.data["uuid"]),
                self.alice,
            )
        self.assertEqual(len(self.client.get(url).data), 3)
        # A room's members and the caller's membership are asked per room;
        # its scope, the step and proposal its name reads, are not.
        self.assertLessEqual(query_count(self.client, self.alice, url) - one, 2 * 4)


# =============================================================================
# Steps of the offerings' teams, the token on the startable list, closed
# rooms still active, and what each member takes part as
# =============================================================================


@override_config(**MATRIX)
class OfferingTeamStepTest(DiscussionTestBase):
    """A step done by the requested offerings' teams has no discussion: on a
    proposal requesting several offerings, one room would hold the teams of
    every one."""

    def offering_team_steps(self):
        source = ContributorSources.REQUESTED_OFFERING_TEAM
        return (
            dict(contributor_source=source, per_requested_offering=True),
            dict(contributor_source=source),
            dict(completer=ResponsibleRoles.OFFERING_MANAGER),
        )

    def test_the_model_refuses_it(self):
        for values in self.offering_team_steps():
            step = models.CallWorkflowStep(
                call=self.call,
                key="technical",
                kind=StepKinds.ASSESSMENT,
                name="Technical",
                order=5,
                outcome_set=OutcomeSets.DONE,
                has_discussion=True,
                **values,
            )
            with self.assertRaises(DjangoValidationError, msg=values) as refusal:
                step.clean()
            self.assertIn("has_discussion", refusal.exception.message_dict)

    def test_the_api_refuses_it(self):
        # A step no proposal stands on yet, whose completer may still change.
        step = self.add_step(
            "technical",
            StepKinds.ASSESSMENT,
            5,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.CALL_MANAGER,
            has_discussion=True,
        )
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            self.step_url(step),
            {"completer": ResponsibleRoles.OFFERING_MANAGER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("has_discussion", response.data)
        step.refresh_from_db()
        self.assertEqual(step.completer, ResponsibleRoles.CALL_MANAGER)


@override_config(**MATRIX, PAT_ENABLED=True)
class StartableTokenTest(DiscussionTestBase):
    """A personal access token lists only what it could open."""

    def setUp(self):
        super().setUp()
        self.alice.can_use_personal_access_tokens = True
        self.alice.save()

    def listed(self, *permissions, bindings=()):
        pat = _create_pat(
            self.alice, scopes=[p.value for p in permissions], bindings=bindings
        )
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get(STARTABLE_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row["proposal_step_uuid"] for row in response.data]

    def test_held_to_its_scopes_and_bindings(self):
        elsewhere = structure_factories.CustomerFactory()
        self.assertEqual(self.listed(PermissionEnum.LIST_CALLS), [])
        self.assertEqual(
            self.listed(PermissionEnum.LIST_PROPOSALS, bindings=[elsewhere]), []
        )
        self.assertEqual(
            self.listed(PermissionEnum.LIST_PROPOSALS, bindings=[self.proposal]),
            [self.instance.uuid.hex],
        )
        self.assertEqual(
            self.listed(PermissionEnum.LIST_PROPOSALS), [self.instance.uuid.hex]
        )


@override_config(**MATRIX)
class ClosedRoomResyncTest(DiscussionTestBase):
    """A closed discussion whose room is still active -- its closing export
    failed -- is resynced on a change of who belongs in it, so that whoever
    has since been kept away leaves the room at once."""

    def setUp(self):
        super().setUp()
        self.discussion = self.open_discussion()
        self.room = self.activate_room(self.discussion, self.alice, self.bob)
        with mock.patch(DISABLE_ROOM):
            step_discussions.close(self.discussion)

    def resynced_on_a_conflict(self):
        profile = factories.ReviewerProfileFactory(user=self.bob)
        with mock.patch(RESYNC) as resync:
            with transaction.atomic():
                factories.ConflictOfInterestFactory(
                    reviewer=profile, proposal=self.proposal
                )
        return [call.args[0].pk for call in resync.call_args_list]

    def test_while_its_room_is_active(self):
        matrix_models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=matrix_models.RoomStates.ACTIVE
        )
        self.assertIn(self.discussion.pk, self.resynced_on_a_conflict())

    def test_not_once_its_room_is_archived(self):
        matrix_models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=matrix_models.RoomStates.ARCHIVED
        )
        self.assertNotIn(self.discussion.pk, self.resynced_on_a_conflict())


@override_config(**MATRIX)
class MemberRoleTest(DiscussionTestBase):
    """What each member takes part as, shown beside their name."""

    def setUp(self):
        super().setUp()
        self.assessment.discussion_panel_members = True
        self.assessment.save(update_fields=["discussion_panel_members"])
        self.discussion = self.open_discussion()

    def test_each_participant_by_what_brings_them_in(self):
        roles = step_discussions.member_roles(self.discussion)
        self.assertEqual(roles[self.alice.pk], "Rapporteur")
        self.assertEqual(roles[self.bob.pk], "Rapporteur")
        self.assertEqual(roles[self.manager.pk], "Call manager")
        self.assertEqual(roles[self.outsider.pk], "Panel member")
        self.assertNotIn(self.applicant.pk, roles)

    def test_the_panel_chair_first(self):
        self.call.panel_chair = self.outsider
        self.call.save(update_fields=["panel_chair"])
        roles = step_discussions.member_roles(self.discussion)
        self.assertEqual(roles[self.outsider.pk], "Panel chair")

    def test_the_member_list_says_it(self):
        room = self.activate_room(self.discussion, self.alice, self.manager)
        self.client.force_authenticate(self.alice)
        response = self.client.get(f"/api/matrix/rooms/{room.uuid.hex}/members/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {row["user_uuid"]: row["role_label"] for row in response.data},
            {self.alice.uuid: "Rapporteur", self.manager.uuid: "Call manager"},
        )

    def test_a_member_joining_later_is_introduced_by_it(self):
        room = self.activate_room(self.discussion)
        with mock.patch.object(matrix_tasks, "post_as_bot") as post:
            matrix_tasks._announce_role(
                room, self.alice, "@alice:matrix.example.com", "Rapporteur", False
            )
        self.assertEqual(
            post.call_args.args[1],
            f"{self.alice.full_name} joins the discussion as Rapporteur.",
        )
