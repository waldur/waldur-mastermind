"""Changes made to what the applicant submitted, or decided for them, by
someone outside the applicant team: refused where nobody should make them,
otherwise recorded with who made them, and told to whom it concerns."""

from django.contrib.admin.sites import AdminSite
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import admin as proposal_admin
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    NotificationRuleTriggers,
    ProposalStates,
    ResponsibleRoles,
    StepKinds,
    WorkflowStepInstanceStatuses,
)

from . import factories
from .test_awarded_resources import AwardedResourcesBase


def events(event_type, proposal):
    return Event.objects.filter(
        event_type=event_type,
        id__in=Feed.objects.filter(
            content_type=ContentType.objects.get_for_model(models.Proposal),
            object_id=proposal.id,
        ).values("event_id"),
    ).order_by("created")


class AwardChangesTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        self._start_allocation()
        self.client.force_authenticate(self.call_manager)

    def test_an_edit_is_recorded_with_who_and_what_it_was(self):
        awarded = self._awarded()
        response = self.client.patch(
            self._detail_url(awarded), {"limits": {"cpu": 40}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        awarded.refresh_from_db()
        self.assertEqual(awarded.modified_by, self.call_manager)
        self.assertEqual(response.data["modified_by_name"], self.call_manager.full_name)
        (event,) = events(EventType.PROPOSAL_AWARD_CHANGED, self.proposal)
        self.assertEqual(event.context["before"]["limits"], {"cpu": 100})
        self.assertEqual(event.context["after"]["limits"], {"cpu": 40})

    def test_adding_and_removing_are_recorded(self):
        response = self.client.post(
            self._list_url(),
            {
                "requested_offering_uuid": self.other_requested_offering.uuid.hex,
                "limits": {"cpu": 10},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        added = self.proposal.awarded_resources.get(uuid=response.data["uuid"])
        response = self.client.delete(self._detail_url(added))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            [
                e.context["change"]
                for e in events(EventType.PROPOSAL_AWARD_CHANGED, self.proposal)
            ],
            ["added", "removed"],
        )

    def test_nobody_changes_their_own_proposals_award(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.proposal.add_user(staff, ProposalRole.MEMBER)
        self.client.force_authenticate(staff)
        response = self.client.patch(
            self._detail_url(self._awarded()), {"limits": {"cpu": 1}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_applicant_does_not_read_the_award_changes(self):
        self.client.patch(
            self._detail_url(self._awarded()), {"limits": {"cpu": 40}}, format="json"
        )
        self.client.force_authenticate(self.applicant)
        response = self.client.get(
            "http://testserver" + reverse("event-list"),
            {
                "scope": factories.ProposalFactory.get_url(self.proposal),
                "page_size": 200,
            },
        )
        self.assertNotIn(
            EventType.PROPOSAL_AWARD_CHANGED,
            [row["event_type"] for row in response.data],
        )


class AnswerForApplicantTest(AwardedResourcesBase):
    """Staff answer the applicant's own step for them."""

    def setUp(self):
        super().setUp()
        self.admin_instance.call_step.kind = StepKinds.APPLICANT_ACTION
        self.admin_instance.call_step.completer = ResponsibleRoles.APPLICANT
        self.admin_instance.call_step.save()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )

    def complete(self, reason=""):
        outcomes = self.admin_instance.call_step.allowed_outcomes
        return self.client.post(
            self.url,
            {
                "step_uuid": self.admin_instance.uuid.hex,
                "outcome": outcomes[0],
                "outcome_reason": reason,
            },
        )

    def test_it_needs_a_reason(self):
        response = self.complete()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("outcome_reason", response.data)

    def test_a_step_the_call_completes_itself_needs_no_reason(self):
        # The call gives this step of the applicant's kind to its own team:
        # completing it answers nothing for the applicant.
        self.admin_instance.call_step.completer = ResponsibleRoles.CALL_MANAGER
        self.admin_instance.call_step.save()
        response = self.complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        (event,) = events(EventType.PROPOSAL_APPLICANT_RESPONDED, self.proposal)
        self.assertNotIn("on behalf of the applicant", event.message)
        self.assertIn("completed step", event.message)

    def test_the_applicant_is_told_who_answered_and_why(self):
        response = self.complete("The applicant asked us by mail.")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        (event,) = events(EventType.PROPOSAL_APPLICANT_RESPONDED, self.proposal)
        self.assertIn("on behalf of the applicant", event.message)
        self.assertIn("The applicant asked us by mail.", event.message)
        self.assertTrue(event.context["on_behalf_of_applicant"])


class ExpiredApplicantStepTest(AwardedResourcesBase):
    def test_an_expired_step_names_nobody_as_answering(self):
        from waldur_mastermind.proposal import activity

        self.admin_instance.call_step.kind = StepKinds.APPLICANT_ACTION
        self.admin_instance.call_step.completer = ResponsibleRoles.APPLICANT
        self.admin_instance.call_step.save()
        self.admin_instance.status = WorkflowStepInstanceStatuses.EXPIRED
        self.admin_instance.save()
        activity.log_step_event(
            self.admin_instance, NotificationRuleTriggers.STEP_EXPIRED
        )
        (event,) = events(EventType.PROPOSAL_APPLICANT_RESPONDED, self.proposal)
        self.assertIn("expired without the applicant's answer", event.message)


class EditsForApplicantTest(AwardedResourcesBase):
    def test_a_staff_edit_of_a_submitted_proposal_is_told_to_the_applicant(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="attach_document"),
            {"file": SimpleUploadedFile("plan.pdf", b"%PDF-1.4\n%%EOF\n")},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        document = models.ProposalDocumentation.objects.get(proposal=self.proposal)
        self.assertEqual(document.uploaded_by, staff)
        (event,) = events(EventType.PROPOSAL_DOCUMENT_ADDED, self.proposal)
        self.assertIn("plan", event.message)
        self.assertIn(staff.full_name, event.message)


class DraftsCanceledAtRoundCloseTest(AwardedResourcesBase):
    def test_each_canceled_draft_is_told_why(self):
        from waldur_mastermind.proposal import utils

        draft = factories.ProposalFactory(
            round=self.proposal.round, state=ProposalStates.DRAFT
        )
        utils.process_closed_round(self.proposal.round)
        draft.refresh_from_db()
        self.assertEqual(draft.state, ProposalStates.CANCELED)
        (event,) = events(EventType.PROPOSAL_CANCELED, draft)
        self.assertIn("closed before it was submitted", event.message)


class AdminTest(AwardedResourcesBase):
    def test_a_submitted_proposal_is_read_only_in_the_admin(self):
        site = AdminSite()
        admin = proposal_admin.ProposalAdmin(models.Proposal, site)
        request = type(
            "Request",
            (),
            {"user": structure_factories.UserFactory(is_staff=True, is_superuser=True)},
        )()
        self.assertIn("name", admin.get_readonly_fields(request, self.proposal))
        inline = proposal_admin.RequestedResourceInline(models.Proposal, site)
        self.assertFalse(inline.has_change_permission(request, self.proposal))
        self.assertFalse(inline.has_delete_permission(request, self.proposal))
