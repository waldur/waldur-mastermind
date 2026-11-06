"""Adopting a round's results before they are published, and who may record it."""

from datetime import date
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core import mail
from rest_framework import status

from waldur_core.core.models import Notification
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import (
    ResultsPublication,
    RoundLifecycleStates,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_round_lifecycle import (
    DECISION_EMAIL,
    STEP_EVENT,
    LifecycleTestBase,
)

ADOPTION_MAIL = (
    "waldur_mastermind.proposal.tasks.notify_call_managers_about_round_adoption.delay"
)
ADOPTION = {"adopted_at": "2026-09-15", "adoption_note": "Governing Board decision"}


def adoption_events():
    return Event.objects.filter(event_type="round_results_adopted")


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class AdoptionRequiredTest(LifecycleTestBase):
    def setUp(self):
        super().setUp()
        self.call.adoption_required = True
        self.call.save(update_fields=["adoption_required"])

    def test_publication_waits_for_the_adoption(self, decision_email, step_event):
        self.round_action("start_deciding")

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.DECIDING)

    def test_force_does_not_bypass_the_adoption(self, decision_email, step_event):
        self.make_decidable()
        self.round_action("start_deciding")

        response = self.publish({"force": True, "reason": "Board meets later"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("undecided_count", response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.DECIDING)

    def test_publication_follows_the_adoption(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.round_action("start_deciding")
        with mock.patch(ADOPTION_MAIL):
            self.assertEqual(
                self.round_action("record_adoption", ADOPTION).status_code,
                status.HTTP_200_OK,
            )

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )

    def test_adoption_is_taken_only_while_deciding(self, decision_email, step_event):
        with mock.patch(ADOPTION_MAIL) as adoption_mail:
            response = self.round_action("record_adoption", ADOPTION)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(self.round.adopted_at)
        self.assertFalse(adoption_events().exists())
        adoption_mail.assert_not_called()

    def test_adoption_is_refused_once_published(self, decision_email, step_event):
        self.round_action("start_deciding")
        with mock.patch(ADOPTION_MAIL):
            self.round_action("record_adoption", ADOPTION)
        self.publish()

        response = self.round_action(
            "record_adoption", {"adopted_at": "2026-10-01", "adoption_note": "Later"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.round.adopted_at, date(2026, 9, 15))
        self.assertEqual(self.round.adoption_note, "Governing Board decision")

    def test_adoption_is_logged_and_announced_once(self, decision_email, step_event):
        self.round_action("start_deciding")

        with mock.patch(ADOPTION_MAIL) as adoption_mail:
            first = self.round_action("record_adoption", ADOPTION)
            corrected = self.round_action(
                "record_adoption",
                {"adopted_at": "2026-09-16", "adoption_note": "Corrected date"},
            )

        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        self.assertEqual(corrected.status_code, status.HTTP_200_OK, corrected.data)
        self.assertEqual(self.round.adopted_at, date(2026, 9, 16))
        self.assertEqual(adoption_events().count(), 1)
        adoption_mail.assert_called_once_with(
            self.round.uuid.hex, self.call_manager.uuid.hex
        )

    def test_adopted_decision_cannot_be_reopened(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.assertTrue(proposal.decision_held)
        self.round_action("start_deciding")
        with mock.patch(ADOPTION_MAIL):
            self.round_action("record_adoption", ADOPTION)

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(proposal.decision_held)

    def test_proposal_round_summary_carries_the_adoption(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.round_action("start_deciding")
        url = factories.ProposalFactory.get_url(proposal)

        self.client.force_authenticate(self.call_manager)
        self.assertIsNone(self.client.get(url).json()["round"]["adopted_at"])

        with mock.patch(ADOPTION_MAIL):
            self.round_action("record_adoption", ADOPTION)

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["round"]["adopted_at"], "2026-09-15")

    def test_unadopted_decision_can_still_be_reopened(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.round_action("start_deciding")

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(proposal.decision_held)

    def test_call_manager_toggles_the_requirement(self, decision_email, step_event):
        self.client.force_authenticate(self.call_manager)
        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"adoption_required": False},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertFalse(self.call.adoption_required)


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class AdoptionNotRequiredTest(LifecycleTestBase):
    def test_publication_does_not_wait(self, decision_email, step_event):
        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_adoption_is_taken_any_time_after_the_cut_off(
        self, decision_email, step_event
    ):
        with mock.patch(ADOPTION_MAIL):
            response = self.round_action("record_adoption", ADOPTION)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)
        self.assertEqual(self.round.adopted_at, date(2026, 9, 15))


class AdoptionMailTest(LifecycleTestBase):
    def test_call_managers_are_told(self):
        Notification.objects.update_or_create(
            key="proposal.round_results_adopted", defaults={"enabled": True}
        )
        self.round.adopted_at = date(2026, 9, 15)
        self.round.adoption_note = "Board minutes: list adopted unchanged"
        self.round.save(update_fields=["adopted_at", "adoption_note"])

        tasks.notify_call_managers_about_round_adoption(
            self.round.uuid.hex, self.call_manager.uuid.hex
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.call_manager.email])
        self.assertIn(self.round.name, mail.outbox[0].subject)
        # The note is the round's record, read where its readers are checked.
        self.assertNotIn("Board minutes", mail.outbox[0].body)


class AdoptionWriterMustReadItTest(LifecycleTestBase):
    """A round-closer who cannot read the adoption record may not overwrite
    it while the round withholds its results."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch(STEP_EVENT))
        self.enterContext(mock.patch(DECISION_EMAIL))
        self.enterContext(mock.patch(ADOPTION_MAIL))
        role = Role.objects.create(
            name="CALL.ROUND_CLOSER",
            description="Closes rounds",
            content_type=ContentType.objects.get_for_model(models.Call),
        )
        role.add_permission(PermissionEnum.CLOSE_ROUNDS)
        role.add_permission(PermissionEnum.LIST_CALLS)
        self.closer = structure_factories.UserFactory()
        self.call.add_user(self.closer, role)
        self.round.adopted_at = date(2026, 9, 1)
        self.round.adoption_note = "Recorded by the call manager"
        self.round.save(update_fields=["adopted_at", "adoption_note"])

    def test_refused_while_results_are_withheld(self):
        response = self.round_action(
            "record_adoption", {"adopted_at": "2026-09-15"}, user=self.closer
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.round.adopted_at, date(2026, 9, 1))
        self.assertEqual(self.round.adoption_note, "Recorded by the call manager")

    def test_allowed_once_results_are_published(self):
        self.publish()

        response = self.round_action(
            "record_adoption",
            {"adopted_at": "2026-09-15", "adoption_note": "Closer's note"},
            user=self.closer,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.adoption_note, "Closer's note")

    def test_call_manager_unaffected(self):
        response = self.round_action(
            "record_adoption",
            {"adopted_at": "2026-09-15", "adoption_note": "Manager's note"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.adoption_note, "Manager's note")


class ImmediateAdoptionWriterTest(AdoptionWriterMustReadItTest):
    """A call announcing each decision as it is made withholds nothing."""

    publish_results = ResultsPublication.IMMEDIATELY

    def test_refused_while_results_are_withheld(self):
        response = self.round_action(
            "record_adoption", {"adopted_at": "2026-09-15"}, user=self.closer
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
