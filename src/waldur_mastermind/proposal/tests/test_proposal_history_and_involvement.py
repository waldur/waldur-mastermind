"""A proposal's history from its creation, the history a demo preset's
proposals are given, and the lists of the proposals a user takes part in."""

import datetime

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.demo_presets import proposal_history
from waldur_mastermind.proposal import activity, models
from waldur_mastermind.proposal.tests import factories, fixtures


def feed_of(proposal):
    return Event.objects.filter(
        id__in=Feed.objects.filter(
            content_type=ContentType.objects.get_for_model(models.Proposal),
            object_id=proposal.id,
        ).values("event_id")
    ).order_by("created", "id")


class ProposalCreatedEventTest(test.APITestCase):
    def test_creating_a_proposal_logs_it_as_the_applicant_did(self):
        fixture = fixtures.ProposalFixture()
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.post(
            factories.ProposalFactory.get_list_url(),
            {"round_uuid": fixture.round.uuid.hex, "name": "Fresh"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        proposal = models.Proposal.objects.get(uuid=response.data["uuid"])
        created = feed_of(proposal).filter(event_type=EventType.PROPOSAL_CREATED)
        self.assertEqual(created.count(), 1)
        self.assertEqual(created.get().context["user_uuid"], user.uuid.hex)


class PresetProposalHistoryTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft = self.fixture.proposal
        self.submitted = self.fixture.proposal_submitted
        self.applicant = self.draft.created_by
        self.draft.add_user(self.applicant, ProposalRole.MANAGER)
        # A preset writes its proposals and roles without logging anything.
        Event.objects.all().delete()
        self.created_at = timezone.now() - datetime.timedelta(days=5)
        models.Proposal.objects.filter(id=self.draft.id).update(created=self.created_at)

    def test_a_preset_proposal_gets_the_history_its_state_implies(self):
        proposal_history.generate_proposal_history()
        draft = [event.event_type for event in feed_of(self.draft)]
        self.assertEqual(draft[0], EventType.PROPOSAL_CREATED)
        self.assertIn(EventType.ROLE_GRANTED, draft)
        self.assertNotIn(EventType.PROPOSAL_SUBMITTED, draft)
        submitted = [event.event_type for event in feed_of(self.submitted)]
        self.assertIn(EventType.PROPOSAL_SUBMITTED, submitted)

    def test_a_submitted_preset_proposal_says_when_it_was_submitted(self):
        models.Proposal.objects.filter(id=self.submitted.id).update(submitted_at=None)
        proposal_history.generate_proposal_history()
        self.submitted.refresh_from_db()
        self.draft.refresh_from_db()
        logged = feed_of(self.submitted).get(event_type=EventType.PROPOSAL_SUBMITTED)
        self.assertEqual(self.submitted.submitted_at, logged.created)
        self.assertIsNone(self.draft.submitted_at)

    def test_a_submitted_preset_review_says_when_it_was_submitted(self):
        review = self.fixture.review
        models.Review.objects.filter(id=review.id).update(
            state=models.Review.States.SUBMITTED, submitted_at=None
        )
        in_review = factories.ReviewFactory(proposal=self.submitted)
        proposal_history.generate_proposal_history()
        review.refresh_from_db()
        in_review.refresh_from_db()
        self.assertEqual(review.submitted_at, review.modified)
        self.assertIsNone(in_review.submitted_at)

    def test_a_logged_review_submission_dates_the_review(self):
        review = self.fixture.review
        models.Review.objects.filter(id=review.id).update(
            state=models.Review.States.SUBMITTED, submitted_at=None
        )
        logged_at = timezone.now() - datetime.timedelta(days=2)
        before = Event.objects.order_by("-id").values_list("id", flat=True).first()
        activity.log_review_event(review, EventType.REVIEW_SUBMITTED)
        Event.objects.filter(id__gt=before or 0).update(created=logged_at)
        proposal_history.generate_proposal_history()
        review.refresh_from_db()
        self.assertEqual(review.submitted_at, logged_at)

    def test_it_is_dated_and_named_as_the_applicant_did_it(self):
        proposal_history.generate_proposal_history()
        created = feed_of(self.draft).get(event_type=EventType.PROPOSAL_CREATED)
        self.assertEqual(created.created, self.created_at)
        self.assertEqual(created.context["user_uuid"], self.applicant.uuid.hex)

    def test_a_proposal_with_a_history_is_left_alone(self):
        self.assertEqual(proposal_history.generate_proposal_history(), 2)
        logged = Event.objects.count()
        self.assertEqual(proposal_history.generate_proposal_history(), 0)
        self.assertEqual(Event.objects.count(), logged)


class InvolvedProposalsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal_submitted
        self.applicant = self.fixture.proposal_submitted_creator
        self.fixture.requested_resource

    def listed(self, user, url, **query):
        self.client.force_authenticate(user)
        response = self.client.get(url, {"involved": "true", **query})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def proposals(self, user):
        return {
            row["uuid"]
            for row in self.listed(user, factories.ProposalFactory.get_list_url())
        }

    def test_staff_on_no_proposal_list_none_of_their_own(self):
        self.assertEqual(self.proposals(self.fixture.staff), set())

    def test_staff_on_a_proposal_team_list_that_one(self):
        staff = self.fixture.staff
        self.proposal.add_user(staff, ProposalRole.MEMBER)
        self.assertEqual(self.proposals(staff), {self.proposal.uuid.hex})

    def test_it_changes_nothing_for_anyone_else(self):
        for user in (self.applicant, self.fixture.call_manager):
            self.client.force_authenticate(user)
            everything = {
                row["uuid"]
                for row in self.client.get(
                    factories.ProposalFactory.get_list_url()
                ).data
            }
            self.assertEqual(self.proposals(user), everything)
        self.assertIn(self.proposal.uuid.hex, self.proposals(self.applicant))

    def test_the_resource_lens_lists_the_same_proposals(self):
        url = "/api/proposal-my-requested-resources/"
        self.assertEqual(self.listed(self.fixture.staff, url), [])
        self.fixture.proposal.add_user(self.fixture.staff, ProposalRole.MEMBER)
        rows = self.listed(self.fixture.staff, url)
        self.assertEqual(
            {row["proposal_uuid"] for row in rows}, {self.fixture.proposal.uuid.hex}
        )
