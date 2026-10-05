"""Applicant identity reaches a reviewer-only viewer through no path other
than the proposal payload, and there only as far as the call's applicant
visibility config allows."""

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.logging import event_logger
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import EventType
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, serializers
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures

EVENTS_URL = "http://testserver" + reverse("event-list")
EVENTS_COUNT_URL = "http://testserver" + reverse("event-count")


class ApplicantIdentityTestMixin:
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal_submitted
        self.applicant = self.proposal.created_by
        self.reviewer = self.fixture.reviewer_1
        self.call_manager = self.fixture.call_manager
        self.staff = self.fixture.staff
        self.support = self.fixture.global_support
        self.co_applicant = structure_factories.UserFactory(
            email="co-applicant@example.com"
        )
        self.proposal.add_user(
            self.applicant, ProposalRole.MANAGER, created_by=self.applicant
        )
        self.proposal.add_user(
            self.co_applicant, ProposalRole.MEMBER, created_by=self.applicant
        )

    def conceal_identity(self):
        return models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_full_name=False,
            expose_username=False,
            expose_email=False,
        )

    def expose_identity(self):
        return models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_full_name=True,
            expose_username=True,
            expose_email=True,
        )

    def get(self, user, url, params=None):
        self.client.force_authenticate(user)
        response = self.client.get(url, params or {})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def full_data_viewers(self):
        return {
            "call manager": self.call_manager,
            "staff": self.staff,
            "support": self.support,
        }


class RoundListingApplicantIdentityTest(ApplicantIdentityTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.round = self.proposal.round
        self.draft = factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )
        base = factories.CallFactory.get_protected_url(self.call)
        self.list_url = base + "rounds/"
        self.detail_url = base + f"rounds/{self.round.uuid.hex}/"

    def _rows(self, user):
        data = self.get(user, self.list_url)
        rounds = data["results"] if isinstance(data, dict) else data
        listed = [r for r in rounds if r["uuid"] == self.round.uuid.hex]
        self.assertEqual(len(listed), 1)
        detail = self.get(user, self.detail_url)
        self.assertEqual(
            [p["uuid"] for p in listed[0]["proposals"]],
            [p["uuid"] for p in detail["proposals"]],
        )
        return {p["uuid"]: p for p in detail["proposals"]}, listed[0]["proposals"]

    def test_reviewer_does_not_see_creator_names_when_full_name_concealed(self):
        self.conceal_identity()
        detail_rows, list_rows = self._rows(self.reviewer)
        self.assertIn(self.proposal.uuid.hex, detail_rows)
        for row in list(detail_rows.values()) + list_rows:
            self.assertNotIn("created_by_name", row)

    def test_reviewer_sees_creator_name_when_call_exposes_it(self):
        self.expose_identity()
        detail_rows, _ = self._rows(self.reviewer)
        self.assertEqual(
            detail_rows[self.proposal.uuid.hex]["created_by_name"],
            self.applicant.full_name,
        )

    def test_reviewer_does_not_see_draft_proposals(self):
        for configure in (self.conceal_identity, self.expose_identity):
            with self.subTest(config=configure.__name__):
                models.CallApplicantVisibilityConfig.objects.filter(
                    call=self.call
                ).delete()
                configure()
                detail_rows, list_rows = self._rows(self.reviewer)
                self.assertNotIn(self.draft.uuid.hex, detail_rows)
                self.assertNotIn(
                    self.draft.uuid.hex, [row["uuid"] for row in list_rows]
                )
                self.assertIn(self.proposal.uuid.hex, detail_rows)

    def test_live_review_holder_does_not_see_draft_or_names(self):
        self.conceal_identity()
        # A reviewer with a call role and a live review on one proposal.
        detail_rows, _ = self._rows(self.reviewer)
        self.assertTrue(
            models.Review.objects.filter(
                reviewer=self.reviewer, proposal=self.proposal
            ).exists()
        )
        self.assertNotIn(self.draft.uuid.hex, detail_rows)
        self.assertNotIn("created_by_name", detail_rows[self.proposal.uuid.hex])

    def test_reviewer_sees_own_draft_with_name(self):
        self.conceal_identity()
        own_draft = factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT, created_by=self.reviewer
        )
        detail_rows, _ = self._rows(self.reviewer)
        self.assertEqual(
            detail_rows[own_draft.uuid.hex]["created_by_name"],
            self.reviewer.full_name,
        )
        self.assertNotIn(self.draft.uuid.hex, detail_rows)

    def test_full_data_viewers_see_drafts_and_names(self):
        self.conceal_identity()
        for label, user in self.full_data_viewers().items():
            with self.subTest(viewer=label):
                detail_rows, _ = self._rows(user)
                self.assertIn(self.draft.uuid.hex, detail_rows)
                self.assertEqual(
                    detail_rows[self.proposal.uuid.hex]["created_by_name"],
                    self.applicant.full_name,
                )

    def test_call_manager_who_is_also_reviewer_sees_full_data(self):
        self.conceal_identity()
        self.call.add_user(self.call_manager, CallRole.REVIEWER)
        detail_rows, _ = self._rows(self.call_manager)
        self.assertIn(self.draft.uuid.hex, detail_rows)
        self.assertIn("created_by_name", detail_rows[self.proposal.uuid.hex])


class RoundListingQueryCountTest(ApplicantIdentityTestMixin, test.APITestCase):
    """Resolving what each viewer may see of a round's proposals costs the
    same whatever the number of proposals."""

    def setUp(self):
        super().setUp()
        self.conceal_identity()
        self.round = self.proposal.round
        self.url = (
            factories.CallFactory.get_protected_url(self.call)
            + f"rounds/{self.round.uuid.hex}/"
        )

    def _queries(self, user):
        self.client.force_authenticate(user)
        # Reviews are resolved per proposal by a separate, unrelated field.
        with mock.patch.object(
            serializers.ProtectedProposalListSerializer,
            "get_reviews",
            return_value=[],
        ):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(queries)

    def _add_proposals(self, count):
        for _ in range(count):
            factories.ProposalFactory(round=self.round, state=ProposalStates.SUBMITTED)
            factories.ProposalFactory(round=self.round, state=ProposalStates.DRAFT)

    def test_query_count_does_not_grow_with_proposals(self):
        for label, user in {
            "reviewer": self.reviewer,
            "call manager": self.call_manager,
        }.items():
            with self.subTest(viewer=label):
                self._add_proposals(1)
                # The first request may seed system roles; measure after it.
                self._queries(user)
                few = self._queries(user)
                self._add_proposals(5)
                many = self._queries(user)
                self.assertEqual(few, many)


class ProposalCreatorFilterApplicantIdentityTest(
    ApplicantIdentityTestMixin, test.APITestCase
):
    """Filtering proposals by their creator would confirm authorship to an
    evaluator from whom the call conceals the applicant's identity."""

    def setUp(self):
        super().setUp()
        self.url = factories.ProposalFactory.get_list_url()

    def _uuids(self, user, creator):
        data = self.get(user, self.url, {"created_by_uuid": creator.uuid.hex})
        rows = data["results"] if isinstance(data, dict) else data
        return {row["uuid"] for row in rows}

    def test_reviewer_cannot_confirm_authorship_when_username_concealed(self):
        self.conceal_identity()
        self.assertNotIn(
            self.proposal.uuid.hex, self._uuids(self.reviewer, self.applicant)
        )

    def test_reviewer_can_filter_when_username_exposed(self):
        self.expose_identity()
        self.assertIn(
            self.proposal.uuid.hex, self._uuids(self.reviewer, self.applicant)
        )

    def test_reviewer_finds_own_proposals(self):
        self.conceal_identity()
        own = factories.ProposalFactory(
            round=self.proposal.round,
            state=ProposalStates.SUBMITTED,
            created_by=self.reviewer,
        )
        self.assertEqual(self._uuids(self.reviewer, self.reviewer), {own.uuid.hex})

    def test_full_data_viewers_can_filter(self):
        self.conceal_identity()
        for label, user in {
            **self.full_data_viewers(),
            "applicant": self.applicant,
        }.items():
            with self.subTest(viewer=label):
                self.assertIn(self.proposal.uuid.hex, self._uuids(user, self.applicant))


class ProposalResourceApplicantIdentityTest(
    ApplicantIdentityTestMixin, test.APITestCase
):
    def setUp(self):
        super().setUp()
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
            created_by=self.applicant,
        )
        self.list_url = factories.RequestedResourceFactory.get_list_url(self.proposal)
        self.detail_url = factories.RequestedResourceFactory.get_url(
            self.proposal, self.requested_resource
        )

    def _rows(self, user):
        data = self.get(user, self.list_url)
        rows = data["results"] if isinstance(data, dict) else data
        self.assertEqual(len(rows), 1)
        return rows + [self.get(user, self.detail_url)]

    def test_reviewer_does_not_see_creator_when_concealed(self):
        self.conceal_identity()
        for row in self._rows(self.reviewer):
            self.assertNotIn("created_by", row)
            self.assertNotIn("created_by_name", row)
            # The request itself is still there to evaluate.
            self.assertIn("attributes", row)

    def test_reviewer_sees_only_what_the_call_exposes(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_full_name=True, expose_username=False
        )
        for row in self._rows(self.reviewer):
            self.assertNotIn("created_by", row)
            self.assertEqual(row["created_by_name"], self.applicant.full_name)

    def test_reviewer_sees_creator_when_exposed(self):
        self.expose_identity()
        for row in self._rows(self.reviewer):
            self.assertIn("created_by", row)
            self.assertIn("created_by_name", row)

    def test_full_data_viewers_and_applicant_team_see_creator(self):
        self.conceal_identity()
        viewers = {
            **self.full_data_viewers(),
            "applicant": self.applicant,
            "co-applicant": self.co_applicant,
        }
        for label, user in viewers.items():
            with self.subTest(viewer=label):
                for row in self._rows(user):
                    self.assertEqual(row["created_by_name"], self.applicant.full_name)
                    self.assertIn(self.applicant.uuid.hex, row["created_by"])


class ChecklistAnswerApplicantIdentityTest(
    ApplicantIdentityTestMixin, test.APITestCase
):
    def setUp(self):
        super().setUp()
        ProposalRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL)
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=checklist,
            question_type=checklist_enums.QuestionTypes.BOOLEAN,
            required=True,
        )
        self.call.compliance_checklist = checklist
        self.call.save()
        completion = self.proposal.checklist_completion
        if not completion:
            completion = checklist_models.ChecklistCompletion.objects.create(
                scope_content_type=ContentType.objects.get_for_model(self.proposal),
                scope_object_id=self.proposal.id,
                checklist=checklist,
            )
        checklist_models.Answer.objects.create(
            completion=completion,
            question=self.question,
            user=self.co_applicant,
            answer_data=True,
        )
        self.url = factories.ProposalFactory.get_url(self.proposal, action="checklist")

    def _answer(self, user):
        data = self.get(user, self.url, {"include_all": "true"})
        (question,) = [
            q for q in data["questions"] if q["uuid"] == self.question.uuid.hex
        ]
        answer = question["existing_answer"]
        self.assertTrue(answer["answer_data"])
        return answer

    def test_reviewer_does_not_see_answer_author_when_concealed(self):
        self.conceal_identity()
        answer = self._answer(self.reviewer)
        self.assertNotIn("user_name", answer)
        self.assertNotIn("user", answer)

    def test_reviewer_sees_only_what_the_call_exposes(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_full_name=True, expose_username=False
        )
        answer = self._answer(self.reviewer)
        self.assertEqual(answer["user_name"], self.co_applicant.full_name)
        self.assertNotIn("user", answer)

    def test_reviewer_sees_answer_author_when_exposed(self):
        self.expose_identity()
        answer = self._answer(self.reviewer)
        self.assertEqual(answer["user_name"], self.co_applicant.full_name)
        self.assertIn("user", answer)

    def test_full_data_viewers_and_applicant_see_answer_author(self):
        self.conceal_identity()
        # Only these read the checklist at all.
        viewers = {
            "call manager": self.call_manager,
            "staff": self.staff,
            "applicant": self.applicant,
        }
        for label, user in viewers.items():
            with self.subTest(viewer=label):
                answer = self._answer(user)
                self.assertEqual(answer["user_name"], self.co_applicant.full_name)
                self.assertIn("user", answer)


class ProposalEventsApplicantIdentityTest(ApplicantIdentityTestMixin, test.APITestCase):
    """Proposal-scoped events are team administration: role grants and
    revocations and invitations. Their messages carry the names of the member
    and of whoever granted the role, and their context the actor's name,
    username, uuid, IP address and user agent -- none of it covered by the
    call's visibility config and none of it needed to evaluate the proposal."""

    def setUp(self):
        super().setUp()
        self.scope_url = factories.ProposalFactory.get_url(self.proposal)
        event_logger.emit(
            "Invitation to {invitation_email} for {scope_name} has been deleted.",
            event_type=EventType.USER_INVITATION_DELETED,
            event_context={
                "invitation_email": "invitee@example.com",
                "scope_name": self.proposal.name,
            },
            scopes=[self.proposal],
        )

    def _events(self, user):
        data = self.get(user, EVENTS_URL, {"scope": self.scope_url})
        return data["results"] if isinstance(data, dict) else data

    def _count(self, user):
        return self.get(user, EVENTS_COUNT_URL, {"scope": self.scope_url})["count"]

    def test_full_data_viewers_see_team_events(self):
        # Guards the premise: the feed does carry the identities.
        self.conceal_identity()
        for label, user in {
            **self.full_data_viewers(),
            "applicant": self.applicant,
        }.items():
            with self.subTest(viewer=label):
                events = self._events(user)
                event_types = {event["event_type"] for event in events}
                self.assertIn(EventType.ROLE_GRANTED, event_types)
                self.assertIn(EventType.USER_INVITATION_DELETED, event_types)
                granted = [
                    e
                    for e in events
                    if e["event_type"] == EventType.ROLE_GRANTED
                    and e["context"].get("affected_user_uuid")
                    == self.co_applicant.uuid.hex
                ]
                self.assertTrue(granted)
                self.assertIn(self.co_applicant.full_name, granted[0]["message"])
                self.assertEqual(self._count(user), len(events))

    def test_reviewer_does_not_see_team_events(self):
        for configure in (self.conceal_identity, self.expose_identity):
            with self.subTest(config=configure.__name__):
                models.CallApplicantVisibilityConfig.objects.filter(
                    call=self.call
                ).delete()
                configure()
                self.assertEqual(self._events(self.reviewer), [])
                self.assertEqual(self._count(self.reviewer), 0)

    def test_live_review_holder_without_call_role_does_not_see_team_events(self):
        self.conceal_identity()
        reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)
        self.assertEqual(self._events(reviewer), [])
        self.assertEqual(self._count(reviewer), 0)

    def test_call_manager_who_is_also_reviewer_sees_team_events(self):
        self.conceal_identity()
        self.call.add_user(self.call_manager, CallRole.REVIEWER)
        self.assertTrue(self._events(self.call_manager))


class ProposalEventDeliveryApplicantIdentityTest(
    ApplicantIdentityTestMixin, test.APITestCase
):
    """The proposal feed stays closed to evaluators on every route an event
    takes: hooks, and the feeds of the proposal's ancestors."""

    def setUp(self):
        super().setUp()
        self.conceal_identity()
        self.event = (
            logging_models.Event.objects.filter(
                event_type=EventType.ROLE_GRANTED,
                context__affected_user_uuid=self.co_applicant.uuid.hex,
            )
            .order_by("-created")
            .first()
        )
        self.assertIsNotNone(self.event)

    def _hook(self, user):
        return logging_factories.WebHookFactory(
            user=user, event_types=[EventType.ROLE_GRANTED]
        )

    def test_evaluator_hook_does_not_match_proposal_team_events(self):
        for label, user in {
            "call reviewer": self.reviewer,
            "live-review holder": factories.ReviewFactory(
                proposal=self.proposal
            ).reviewer,
        }.items():
            with self.subTest(viewer=label):
                hook = self._hook(user)
                self.assertFalse(logging_tasks.check_event(self.event, hook))
                self.assertNotIn(hook, logging_tasks.get_matching_hooks(self.event))

    def test_full_data_viewer_hook_matches_proposal_team_events(self):
        for label, user in {
            "call manager": self.call_manager,
            "applicant": self.applicant,
            "staff": self.staff,
        }.items():
            with self.subTest(viewer=label):
                self.assertTrue(logging_tasks.check_event(self.event, self._hook(user)))

    def test_proposal_team_events_are_not_filed_on_the_organiser_feed(self):
        # The organiser's customer feed is read by its owners, who need not
        # be call managers -- an owner may review on the organisation's call.
        owner = self.fixture.owner
        self.call.add_user(owner, CallRole.REVIEWER)
        customer = self.call.manager.customer
        self.assertFalse(
            logging_models.Feed.objects.filter(
                event=self.event,
                content_type=ContentType.objects.get_for_model(customer),
                object_id=customer.id,
            ).exists()
        )
        events = self.get(
            owner,
            EVENTS_URL,
            {"scope": structure_factories.CustomerFactory.get_url(customer)},
        )
        events = events["results"] if isinstance(events, dict) else events
        for event in events:
            self.assertNotIn(self.co_applicant.full_name, event["message"])
            self.assertNotEqual(
                event["context"].get("affected_user_uuid"), self.co_applicant.uuid.hex
            )

    def test_enriched_proposal_team_event_is_filed_on_the_proposal_feed_only(self):
        # The team change on a submitted proposal is described as one enriched
        # role event; the enrichment must not widen where that event is filed.
        self.assertTrue(self.event.context["after_submission"])
        self.assertEqual(self.event.context["proposal_state"], self.proposal.state)
        self.assertIn("(after submission)", self.event.message)
        feeds = logging_models.Feed.objects.filter(event=self.event)
        self.assertEqual(
            {(feed.content_type_id, feed.object_id) for feed in feeds},
            {
                (
                    ContentType.objects.get_for_model(models.Proposal).id,
                    self.proposal.id,
                )
            },
        )

    def test_call_team_role_events_still_reach_the_organiser_feed(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.REVIEWER)
        customer = self.call.manager.customer
        self.assertTrue(
            logging_models.Feed.objects.filter(
                content_type=ContentType.objects.get_for_model(customer),
                object_id=customer.id,
                event__event_type=EventType.ROLE_GRANTED,
                event__context__affected_user_uuid=user.uuid.hex,
            ).exists()
        )


class WorkflowStepActorApplicantIdentityTest(
    ApplicantIdentityTestMixin, test.APITestCase
):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.admin_step = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.COMPLETED,
            started_at=now,
            completed_at=now,
            completed_by=self.call_manager,
        )
        self.award_step = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="award_response",
            status=WorkflowStepInstanceStatuses.COMPLETED,
            started_at=now,
            completed_at=now,
            completed_by=self.co_applicant,
        )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="workflow_states"
        )

    def _completed_by(self, user):
        data = self.get(user, self.url)
        return {row["step"]: row["completed_by"] for row in data}

    def test_reviewer_does_not_see_applicant_completer_when_concealed(self):
        self.conceal_identity()
        completed_by = self._completed_by(self.reviewer)
        self.assertIsNone(completed_by["award_response"])
        # Call-team actors are not applicant identity.
        self.assertEqual(
            str(completed_by["administrative_check"]).replace("-", ""),
            self.call_manager.uuid.hex,
        )

    def test_reviewer_does_not_see_applicant_author_on_any_step(self):
        self.conceal_identity()
        self.admin_step.completed_by = self.applicant
        self.admin_step.save()
        self.assertIsNone(self._completed_by(self.reviewer)["administrative_check"])

    def test_reviewer_sees_applicant_completer_when_exposed(self):
        self.expose_identity()
        completed_by = self._completed_by(self.reviewer)
        self.assertEqual(
            str(completed_by["award_response"]).replace("-", ""),
            self.co_applicant.uuid.hex,
        )

    def test_call_manager_and_staff_see_applicant_completer(self):
        self.conceal_identity()
        for label, user in {
            "call manager": self.call_manager,
            "staff": self.staff,
        }.items():
            with self.subTest(viewer=label):
                completed_by = self._completed_by(user)
                self.assertEqual(
                    str(completed_by["award_response"]).replace("-", ""),
                    self.co_applicant.uuid.hex,
                )
