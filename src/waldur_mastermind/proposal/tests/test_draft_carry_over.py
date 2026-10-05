"""A draft left open at a round's cut-off moves on to the call's next round."""

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.permissions.utils import get_users
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks, utils, views
from waldur_mastermind.proposal.enums import CallStates, ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


class CarryOverTestBase(test.APITestCase):
    def setUp(self):
        # A call of its own: the shared fixture's rounds would be targets too.
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        now = timezone.now()
        self.ended_round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now + timedelta(days=1),
        )
        self.draft = factories.ProposalFactory(
            round=self.ended_round, state=ProposalStates.DRAFT
        )
        self.draft.add_user(self.draft.created_by, ProposalRole.MANAGER)

    def end_round(self, call_round=None):
        call_round = call_round or self.ended_round
        call_round.cutoff_time = timezone.now() - timedelta(minutes=1)
        call_round.save(update_fields=["cutoff_time"])

    def add_round(self, start_in, cutoff_in):
        now = timezone.now()
        return factories.RoundFactory(
            call=self.call,
            start_time=now + start_in,
            cutoff_time=now + cutoff_in,
        )

    def set_carry_over(self, value):
        self.call.carry_over_drafts = value
        self.call.save(update_fields=["carry_over_drafts"])

    def run_task(self):
        with self.captureOnCommitCallbacks(execute=True):
            tasks.proposals_for_ended_rounds_should_be_cancelled()
        self.draft.refresh_from_db()


class CarryOverTaskTest(CarryOverTestBase):
    def test_new_calls_carry_drafts_over(self):
        self.assertTrue(factories.CallFactory().carry_over_drafts)

    def test_draft_moves_to_the_round_that_follows_the_cutoff(self):
        self.end_round()
        next_round = self.add_round(timedelta(minutes=-1), timedelta(days=30))

        self.run_task()

        self.assertEqual(self.draft.state, ProposalStates.DRAFT)
        self.assertEqual(self.draft.round, next_round)

    def test_draft_keeps_its_content_team_documents_and_resources(self):
        requested_offering = factories.RequestedOfferingFactory(call=self.call)
        resource = factories.RequestedResourceFactory(
            proposal=self.draft, requested_offering=requested_offering
        )
        document = models.ProposalDocumentation.objects.create(proposal=self.draft)
        member = structure_factories.UserFactory()
        self.draft.add_user(member, ProposalRole.MEMBER)
        summary, slug = self.draft.project_summary, self.draft.slug
        self.end_round()
        next_round = self.add_round(timedelta(days=2), timedelta(days=30))

        self.run_task()

        self.assertEqual(self.draft.round, next_round)
        self.assertEqual(self.draft.project_summary, summary)
        # The identifier is not reissued by a move: a final id is only
        # assigned at submission.
        self.assertEqual(self.draft.slug, slug)
        self.assertEqual(list(self.draft.requestedresource_set.all()), [resource])
        self.assertEqual(
            list(models.ProposalDocumentation.objects.filter(proposal=self.draft)),
            [document],
        )
        self.assertEqual(set(get_users(self.draft)), {self.draft.created_by, member})

    def test_earliest_following_round_wins(self):
        self.end_round()
        later = self.add_round(timedelta(days=40), timedelta(days=70))
        earliest = self.add_round(timedelta(days=5), timedelta(days=35))

        self.run_task()

        self.assertEqual(self.draft.round, earliest)
        self.assertNotEqual(self.draft.round, later)

    def test_rounds_that_have_ended_are_not_targets(self):
        self.end_round()
        # Starts after the cut-off but has ended as well, e.g. the sweep ran
        # late: the draft goes to the round still ahead.
        ended_too = factories.RoundFactory(
            call=self.call,
            start_time=self.ended_round.cutoff_time + timedelta(seconds=1),
            cutoff_time=timezone.now() - timedelta(seconds=1),
        )
        ahead = self.add_round(timedelta(days=5), timedelta(days=35))

        self.run_task()

        self.assertEqual(self.draft.round, ahead)
        self.assertNotEqual(self.draft.round, ended_too)

    def test_earlier_rounds_are_not_targets(self):
        factories.RoundFactory(
            call=self.call,
            start_time=timezone.now() - timedelta(days=90),
            cutoff_time=timezone.now() - timedelta(days=60),
        )
        self.end_round()

        self.run_task()

        self.assertEqual(self.draft.state, ProposalStates.CANCELED)
        self.assertEqual(self.draft.round, self.ended_round)

    def test_without_a_later_round_the_draft_is_cancelled(self):
        self.end_round()

        self.run_task()

        self.assertEqual(self.draft.state, ProposalStates.CANCELED)
        self.assertEqual(self.draft.round, self.ended_round)

    def test_with_carry_over_off_the_draft_is_cancelled(self):
        self.set_carry_over(False)
        self.end_round()
        self.add_round(timedelta(days=2), timedelta(days=30))

        self.run_task()

        self.assertEqual(self.draft.state, ProposalStates.CANCELED)
        self.assertEqual(self.draft.round, self.ended_round)

    def test_draft_of_an_archived_call_is_cancelled(self):
        self.end_round()
        self.add_round(timedelta(days=2), timedelta(days=30))
        self.call.state = CallStates.ARCHIVED
        self.call.save(update_fields=["state"])

        self.run_task()

        self.assertEqual(self.draft.state, ProposalStates.CANCELED)

    def test_submitted_proposals_stay_in_their_round(self):
        submitted = factories.ProposalFactory(
            round=self.ended_round, state=ProposalStates.SUBMITTED
        )
        self.end_round()
        self.add_round(timedelta(days=2), timedelta(days=30))

        self.run_task()

        submitted.refresh_from_db()
        self.assertEqual(submitted.round, self.ended_round)
        self.assertEqual(submitted.state, ProposalStates.SUBMITTED)

    def test_a_moved_draft_is_not_cancelled(self):
        self.end_round()
        self.add_round(timedelta(days=2), timedelta(days=30))

        with mock.patch.object(
            tasks.notify_proposal_creator_about_cancelled_proposal, "apply_async"
        ) as cancelled:
            self.run_task()

        cancelled.assert_not_called()
        self.assertFalse(Event.objects.filter(event_type="proposal_canceled").exists())

    def test_one_event_per_moved_draft(self):
        # A name with braces must not break the event message.
        second = factories.ProposalFactory(
            round=self.ended_round, state=ProposalStates.DRAFT, name="HPC {GPU}"
        )
        self.end_round()
        self.add_round(timedelta(days=2), timedelta(days=30))

        self.run_task()
        self.run_task()

        events = Event.objects.filter(event_type="proposal_draft_carried_over")
        self.assertEqual(events.count(), 2)
        self.assertEqual(
            {event.context["proposal_uuid"] for event in events},
            {self.draft.uuid.hex, second.uuid.hex},
        )


@override_settings(task_always_eager=True)
class CarryOverEmailTest(CarryOverTestBase):
    def setUp(self):
        super().setUp()
        structure_factories.NotificationFactory(
            key="proposal.proposal_draft_carried_over"
        )

    def test_applicant_is_emailed_once_about_the_move(self):
        self.end_round()
        next_round = self.add_round(timedelta(days=2), timedelta(days=30))

        self.run_task()
        # A second sweep finds nothing left to move.
        self.run_task()

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [self.draft.created_by.email])
        self.assertIn(self.draft.name, email.subject)
        self.assertIn(next_round.name, email.body)
        self.assertIn(self.ended_round.name, email.body)

    def test_no_email_when_the_draft_is_cancelled(self):
        self.set_carry_over(False)
        self.end_round()
        self.add_round(timedelta(days=2), timedelta(days=30))

        with mock.patch.object(
            tasks.notify_proposal_creator_about_cancelled_proposal, "apply_async"
        ):
            self.run_task()

        self.assertEqual(mail.outbox, [])


@override_settings(task_always_eager=True)
class CarryOverFailureTest(CarryOverTestBase):
    """A failed carry-over leaves the drafts for the next sweep to move."""

    def setUp(self):
        super().setUp()
        structure_factories.NotificationFactory(
            key="proposal.proposal_draft_carried_over"
        )
        self.end_round()
        self.next_round = self.add_round(timedelta(days=2), timedelta(days=30))

    def test_draft_is_not_cancelled_when_carry_over_fails(self):
        with (
            mock.patch.object(
                utils, "carry_over_drafts", side_effect=RuntimeError("boom")
            ),
            mock.patch.object(
                tasks.notify_proposal_creator_about_cancelled_proposal, "apply_async"
            ) as cancelled,
        ):
            self.run_task()

        cancelled.assert_not_called()
        self.assertEqual(self.draft.state, ProposalStates.DRAFT)
        self.assertEqual(self.draft.round, self.ended_round)

    def test_failure_after_the_move_is_undone_and_retried(self):
        emit = event_logger.emit

        def failing_emit(message, event_type, *args, **kwargs):
            if event_type == EventType.PROPOSAL_DRAFT_CARRIED_OVER:
                raise RuntimeError("boom")
            return emit(message, event_type, *args, **kwargs)

        with mock.patch.object(event_logger, "emit", side_effect=failing_emit):
            self.run_task()

        # Not half-moved: still a draft of the ended round, nobody told.
        self.assertEqual(self.draft.state, ProposalStates.DRAFT)
        self.assertEqual(self.draft.round, self.ended_round)
        self.assertEqual(mail.outbox, [])

        self.run_task()

        self.assertEqual(self.draft.state, ProposalStates.DRAFT)
        self.assertEqual(self.draft.round, self.next_round)
        self.assertEqual(len(mail.outbox), 1)


class CarryOverReminderTest(CarryOverTestBase):
    def test_deadline_reminder_refers_to_the_new_cutoff(self):
        self.end_round()
        next_round = self.add_round(timedelta(days=-1), timedelta(days=2))
        self.run_task()
        # A round after the new one, so missing the new cut-off moves the
        # draft on again rather than cancelling it.
        self.add_round(timedelta(days=3), timedelta(days=30))

        with mock.patch.object(core_utils, "broadcast_mail") as broadcast:
            tasks.notify_proposal_creator_on_submission_deadline_approaching()

        contexts = [
            call.args[2]
            for call in broadcast.call_args_list
            if call.args[1] == "proposal_submission_deadline_approaching"
            and call.args[2]["proposal_name"] == self.draft.name
        ]
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["round_name"], next_round.name)
        self.assertEqual(contexts[0]["deadline_date"], next_round.cutoff_time)
        self.assertTrue(contexts[0]["draft_carries_over"])

    def test_reminder_says_the_draft_is_cancelled_without_a_later_round(self):
        self.ended_round.cutoff_time = timezone.now() + timedelta(days=2)
        self.ended_round.save(update_fields=["cutoff_time"])

        with mock.patch.object(core_utils, "broadcast_mail") as broadcast:
            tasks.notify_proposal_creator_on_submission_deadline_approaching()

        contexts = [
            call.args[2]
            for call in broadcast.call_args_list
            if call.args[2]["proposal_name"] == self.draft.name
        ]
        self.assertEqual(len(contexts), 1)
        self.assertFalse(contexts[0]["draft_carries_over"])


class CarryOverCloseRoundTest(CarryOverTestBase):
    def setUp(self):
        super().setUp()
        self.ended_round.start_time = timezone.now() - timedelta(days=1)
        self.ended_round.save(update_fields=["start_time"])

    def close(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        url = factories.RoundFactory.get_url(self.call, self.ended_round, "close")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.draft.refresh_from_db()

    def test_closing_a_round_moves_its_drafts_on(self):
        next_round = self.add_round(timedelta(days=3), timedelta(days=30))

        self.close()

        self.assertEqual(self.draft.state, ProposalStates.DRAFT)
        self.assertEqual(self.draft.round, next_round)
        self.assertTrue(
            Event.objects.filter(event_type="proposal_draft_carried_over").exists()
        )

    def test_closing_a_round_cancels_drafts_when_carry_over_is_off(self):
        self.set_carry_over(False)
        self.add_round(timedelta(days=3), timedelta(days=30))

        self.close()

        self.assertEqual(self.draft.state, ProposalStates.CANCELED)
        self.assertEqual(self.draft.round, self.ended_round)


class CarryOverSettingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(self.call)

    def test_call_manager_can_switch_carry_over_off(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(self.url, {"carry_over_drafts": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["carry_over_drafts"])
        self.call.refresh_from_db()
        self.assertFalse(self.call.carry_over_drafts)


class SubmitRechecksRoundUnderLockTest(test.APITestCase):
    """A submit that passed its validators before a move lands nowhere closed."""

    def test_submit_into_a_round_that_is_not_open_is_refused(self):
        fixture = fixtures.ProposalFixture()
        scheduled = factories.RoundFactory(
            call=fixture.call,
            start_time=timezone.now() + timedelta(days=2),
            cutoff_time=timezone.now() + timedelta(days=30),
        )
        draft = factories.ProposalFactory(round=scheduled, state=ProposalStates.DRAFT)
        draft.add_user(draft.created_by, ProposalRole.MANAGER)
        self.client.force_authenticate(draft.created_by)

        # Stand in for the race: the validators ran while the draft's round
        # was still open, and the draft was moved on before the lock.
        with mock.patch.object(views.ProposalViewSet, "submit_validators", []):
            response = self.client.post(
                factories.ProposalFactory.get_url(draft, action="submit")
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        draft.refresh_from_db()
        self.assertEqual(draft.state, ProposalStates.DRAFT)
