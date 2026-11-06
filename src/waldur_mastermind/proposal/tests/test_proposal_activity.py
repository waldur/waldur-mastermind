"""A proposal's and a review's activity, read through the events API: each
viewer reads only their part of it."""

import json
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import activity, models
from waldur_mastermind.proposal.enums import ProposalRoleEligibility, ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_round_lifecycle import (
    DECISION_EMAIL,
    STEP_EVENT,
    LifecycleTestBase,
)
from waldur_mastermind.proposal.tests.test_step_inputs import StepInputsTestMixin

EVENTS_URL = "http://testserver" + reverse("event-list")

# The events a workflow run logs that are for the call team and the
# evaluator who acted, never for the applicant team.
EVALUATION_EVENTS = {
    EventType.PROPOSAL_STEP_STARTED,
    EventType.PROPOSAL_STEP_COMPLETED,
    EventType.PROPOSAL_CONTRIBUTION_SUBMITTED,
    EventType.PROPOSAL_REPORT_RELEASED,
    EventType.PROPOSAL_DECISION_HELD,
    EventType.PROPOSAL_EVALUATOR_ASSIGNED,
    EventType.PROPOSAL_EVALUATOR_REVOKED,
    EventType.REVIEW_ASSIGNED,
    EventType.REVIEW_SUBMITTED,
}


class ActivityReaderMixin:
    def read(self, user, scope_url):
        self.client.force_authenticate(user)
        response = self.client.get(EVENTS_URL, {"scope": scope_url, "page_size": 200})
        if response.status_code == status.HTTP_400_BAD_REQUEST:
            # Who cannot read the object cannot name it as a scope.
            return []
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def proposal_activity(self, user, proposal=None):
        return self.read(
            user, factories.ProposalFactory.get_url(proposal or self.proposal)
        )

    def types(self, events):
        return {event["event_type"] for event in events}

    def assert_names_nobody_of(self, events, *users):
        text = json.dumps(events, default=str)
        for user in users:
            self.assertNotIn(user.uuid.hex, text)
            self.assertNotIn(user.username, text)
            if user.full_name:
                self.assertNotIn(user.full_name, text)


class ProposalWorkflowActivityTest(
    ActivityReaderMixin, StepInputsTestMixin, test.APITestCase
):
    """Through the rapporteurs' steps up to the report released to the
    applicant."""

    def setUp(self):
        super().setUp()
        self.provider = self.fixture.offering_fixture.offering_manager
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
            created_by=self.applicant,
        )
        self.outsider = structure_factories.UserFactory()

    def test_workflow_is_logged_on_the_proposal_feed_only(self):
        self.reach_consolidated_step()
        logged = Event.objects.filter(
            event_type__in=[
                EventType.PROPOSAL_SUBMITTED,
                EventType.PROPOSAL_STEP_STARTED,
                EventType.PROPOSAL_STEP_COMPLETED,
                EventType.PROPOSAL_CONTRIBUTION_SUBMITTED,
                EventType.PROPOSAL_APPLICANT_RESPONDED,
            ]
        )
        self.assertEqual(
            set(logged.values_list("event_type", flat=True)),
            {
                EventType.PROPOSAL_SUBMITTED,
                EventType.PROPOSAL_STEP_STARTED,
                EventType.PROPOSAL_STEP_COMPLETED,
                EventType.PROPOSAL_CONTRIBUTION_SUBMITTED,
                EventType.PROPOSAL_APPLICANT_RESPONDED,
            },
        )
        proposal_type = ContentType.objects.get_for_model(models.Proposal)
        for event in logged:
            self.assertEqual(
                list(
                    Feed.objects.filter(event=event).values_list(
                        "content_type", "object_id"
                    )
                ),
                [(proposal_type.id, self.proposal.id)],
            )

    def test_call_manager_without_an_organisation_role_reads_it_all(self):
        self.reach_consolidated_step()
        self.release(self.manager)
        manager_rows = self.proposal_activity(self.manager)
        staff_rows = self.proposal_activity(self.fixture.staff)
        self.assertEqual(
            {row["uuid"] for row in manager_rows}, {row["uuid"] for row in staff_rows}
        )
        self.assertLessEqual(
            {
                EventType.PROPOSAL_SUBMITTED,
                EventType.PROPOSAL_STEP_STARTED,
                EventType.PROPOSAL_STEP_COMPLETED,
                EventType.PROPOSAL_CONTRIBUTION_SUBMITTED,
                EventType.PROPOSAL_APPLICANT_RESPONDED,
                EventType.PROPOSAL_REPORT_RELEASED,
            },
            self.types(manager_rows),
        )

    def test_applicant_team_reads_only_what_it_is_told(self):
        for checkpoint in ("in review", "report released"):
            if checkpoint == "in review":
                self.reach_response_step()
            else:
                self.respond()
                self.contribute(
                    self.rapporteur_1,
                    self.individual_step,
                    self.report_question,
                    "Fund it.",
                )
                self.complete_ok("done")
                self.contribute(
                    self.rapporteur_1,
                    self.consolidated_step,
                    self.report_question,
                    "Fund.",
                )
                self.complete_ok("done")
                self.release(self.manager)
            for label, user in (
                ("creator", self.applicant),
                ("member", self.team_member),
            ):
                with self.subTest(checkpoint=checkpoint, viewer=label):
                    rows = self.proposal_activity(user)
                    self.assertIn(EventType.PROPOSAL_SUBMITTED, self.types(rows))
                    self.assertLessEqual(
                        self.types(rows),
                        activity.APPLICANT_EVENT_TYPES,
                    )
                    self.assertFalse(self.types(rows) & EVALUATION_EVENTS)
                    self.assert_names_nobody_of(
                        rows, self.rapporteur_1, self.rapporteur_2, self.reviewer
                    )
        self.assertIn(
            EventType.PROPOSAL_APPLICANT_RESPONDED,
            self.types(self.proposal_activity(self.applicant)),
        )

    def test_rapporteur_reads_only_their_own_actions(self):
        self.reach_consolidated_step()
        self.release(self.manager)
        rows = self.proposal_activity(self.rapporteur_1)
        self.assertEqual(self.types(rows), {EventType.PROPOSAL_CONTRIBUTION_SUBMITTED})
        for row in rows:
            self.assertEqual(row["context"]["user_uuid"], self.rapporteur_1.uuid.hex)
        self.assertEqual(self.proposal_activity(self.rapporteur_2), [])

    def test_reviewer_and_centre_assessor_read_only_their_own_actions(self):
        self.reach_consolidated_step()
        for user in (self.reviewer, self.provider):
            with self.subTest(user=user.username):
                self.assertEqual(self.proposal_activity(user), [])

    def test_outsider_reads_nothing(self):
        self.reach_consolidated_step()
        self.release(self.manager)
        self.assertEqual(self.proposal_activity(self.outsider), [])

    def test_evaluator_assignment_is_for_the_call_team(self):
        self.submit()
        role = models.CallProposalRole.objects.create(
            call=self.call,
            key="second-reader",
            name="Second reader",
            eligible_from=ProposalRoleEligibility.ANY_USER,
            max_holders=1,
        )
        self.client.force_authenticate(self.fixture.staff)
        evaluator = structure_factories.UserFactory()
        response = self.client.post(
            self.url("role_assignments"),
            {"role_uuid": role.uuid.hex, "user_uuid": evaluator.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn(
            EventType.PROPOSAL_EVALUATOR_ASSIGNED,
            self.types(self.proposal_activity(self.manager)),
        )
        self.assertNotIn(
            EventType.PROPOSAL_EVALUATOR_ASSIGNED,
            self.types(self.proposal_activity(self.applicant)),
        )
        self.assertEqual(self.proposal_activity(evaluator), [])

    # A marketplace-only deployment never shows its applicants a call, and
    # the applicant reads this feed: its messages name the access request.
    @override_config(SERVICE_ACCESS_MODE="marketplace")
    def test_marketplace_mode_names_the_access_request(self):
        self.reach_consolidated_step()
        # The role events name the role by its label, which follows the mode
        # on its own (the roles' alternate descriptions).
        messages = [
            event["message"]
            for event in self.proposal_activity(self.applicant)
            if event["event_type"].startswith("proposal_")
        ]
        self.assertIn(f"Access request {self.proposal.name} was submitted.", messages)
        for message in messages:
            self.assertNotIn(
                "proposal", message.replace(self.proposal.name, "").lower()
            )

    @override_config(SERVICE_ACCESS_MODE="both")
    def test_other_modes_name_the_proposal(self):
        self.reach_consolidated_step()
        messages = [
            event["message"] for event in self.proposal_activity(self.applicant)
        ]
        self.assertIn(f"Proposal {self.proposal.name} was submitted.", messages)

    def test_hooks_deliver_what_the_listing_shows(self):
        self.reach_consolidated_step()
        contribution = Event.objects.filter(
            event_type=EventType.PROPOSAL_CONTRIBUTION_SUBMITTED
        ).first()
        submitted = Event.objects.get(event_type=EventType.PROPOSAL_SUBMITTED)
        for user, event, delivered in (
            (self.manager, contribution, True),
            (self.applicant, contribution, False),
            (self.applicant, submitted, True),
            (self.rapporteur_1, contribution, True),
            (self.rapporteur_1, submitted, False),
            (self.rapporteur_2, contribution, False),
        ):
            hook = logging_factories.WebHookFactory(
                user=user, event_types=[event.event_type]
            )
            with self.subTest(user=user.username, event=event.event_type):
                self.assertEqual(logging_tasks.check_event(event, hook), delivered)


class HeldDecisionActivityTest(ActivityReaderMixin, LifecycleTestBase):
    """A decision held until the round publishes, then published."""

    def setUp(self):
        super().setUp()
        self.proposal = self.make_decidable()
        self.applicant = self.proposal.created_by
        self.member = structure_factories.UserFactory()
        self.proposal.add_user(self.member, ProposalRole.MEMBER)
        self.panel_member = structure_factories.UserFactory()
        self.call.add_user(self.panel_member, CallRole.PANEL_MEMBER)
        self.outsider = structure_factories.UserFactory()

    @mock.patch(STEP_EVENT)
    @mock.patch(DECISION_EMAIL)
    def test_decision_held_then_published(self, decision_email, step_event):
        self.decide(self.proposal, user=self.call_manager)
        self.assertTrue(self.proposal.decision_held)

        manager_types = self.types(self.proposal_activity(self.call_manager))
        self.assertIn(EventType.PROPOSAL_DECISION_HELD, manager_types)
        self.assertNotIn(EventType.PROPOSAL_DECISION_PUBLISHED, manager_types)
        for user in (self.applicant, self.member, self.panel_member, self.outsider):
            with self.subTest(checkpoint="held", user=user.username):
                rows = self.proposal_activity(user)
                self.assertFalse(
                    self.types(rows)
                    & {
                        EventType.PROPOSAL_DECISION_HELD,
                        EventType.PROPOSAL_DECISION_PUBLISHED,
                        EventType.PROPOSAL_STEP_COMPLETED,
                    }
                )

        response = self.publish()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        manager_types = self.types(self.proposal_activity(self.call_manager))
        self.assertLessEqual(
            {
                EventType.PROPOSAL_DECISION_HELD,
                EventType.PROPOSAL_STEP_COMPLETED,
                EventType.PROPOSAL_DECISION_PUBLISHED,
            },
            manager_types,
        )
        for user in (self.applicant, self.member):
            with self.subTest(checkpoint="published", user=user.username):
                rows = self.proposal_activity(user)
                published = [
                    row
                    for row in rows
                    if row["event_type"] == EventType.PROPOSAL_DECISION_PUBLISHED
                ]
                self.assertEqual(len(published), 1)
                # Told the outcome, not who decided it.
                self.assertNotIn("user_uuid", published[0]["context"])
                self.assertLessEqual(self.types(rows), activity.APPLICANT_EVENT_TYPES)
        for user in (self.panel_member, self.outsider):
            with self.subTest(checkpoint="published", user=user.username):
                self.assertEqual(self.proposal_activity(user), [])


class ReviewActivityTest(ActivityReaderMixin, test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        self.applicant = self.proposal.created_by
        self.proposal.add_user(self.applicant, ProposalRole.MANAGER)
        self.reviewer = self.fixture.reviewer_2
        self.outsider = structure_factories.UserFactory()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.ReviewFactory.get_list_url(),
            {
                "proposal": factories.ProposalFactory.get_url(self.proposal),
                "reviewer": structure_factories.UserFactory.get_url(self.reviewer),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.review = models.Review.objects.get(uuid=response.data["uuid"])
        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            factories.ReviewFactory.get_url(self.review, "submit"),
            {"summary_score": 5, "summary_public_comment": "Solid."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review_url = factories.ReviewFactory.get_url(self.review)

    def test_reviewer_and_call_team_read_the_review_history(self):
        for user in (self.reviewer, self.fixture.call_manager, self.fixture.staff):
            with self.subTest(user=user.username):
                rows = self.read(user, self.review_url)
                self.assertEqual(
                    self.types(rows),
                    {EventType.REVIEW_ASSIGNED, EventType.REVIEW_SUBMITTED},
                )

    def test_messages_never_name_the_reviewer(self):
        for event in Event.objects.filter(
            event_type__in=[EventType.REVIEW_ASSIGNED, EventType.REVIEW_SUBMITTED]
        ):
            self.assertNotIn(self.reviewer.username, event.message)
            self.assertNotIn(self.reviewer.full_name, event.message)

    def test_applicant_reads_no_review_history_even_when_shown_the_review(self):
        call = self.proposal.round.call
        call.reviews_visible_to_submitters = True
        call.save()
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        self.client.force_authenticate(self.applicant)
        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.read(self.applicant, self.review_url), [])
        rows = self.proposal_activity(self.applicant)
        self.assertFalse(
            self.types(rows) & {EventType.REVIEW_ASSIGNED, EventType.REVIEW_SUBMITTED}
        )
        self.assert_names_nobody_of(rows, self.reviewer)

    def test_outsider_reads_nothing(self):
        self.assertEqual(self.read(self.outsider, self.review_url), [])

    def test_review_events_are_on_the_proposal_feed_for_the_call_team(self):
        types = self.types(self.proposal_activity(self.fixture.call_manager))
        self.assertLessEqual(
            {EventType.REVIEW_ASSIGNED, EventType.REVIEW_SUBMITTED}, types
        )
        # The reviewer reads what they did on the proposal, not the
        # assignment someone else made.
        self.assertEqual(
            self.types(self.proposal_activity(self.reviewer)),
            {EventType.REVIEW_SUBMITTED},
        )
