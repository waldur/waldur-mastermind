"""A running call's workflow: progress per step, one proposal's timeline, the
paths proposals took, and the log of workflow mail sent."""

import datetime
import json
from unittest import mock

from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    activity,
    models,
    tasks,
    workflow_observability,
)
from waldur_mastermind.proposal import workflow_description as wd
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures

ACTIVE = WorkflowStepInstanceStatuses.ACTIVE
COMPLETED = WorkflowStepInstanceStatuses.COMPLETED
SKIPPED = WorkflowStepInstanceStatuses.SKIPPED


class Base(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.round = self.fixture.round
        self.now = timezone.now()
        steps = list(
            models.CallWorkflowStep.objects.filter(call=self.call).order_by(
                "order", "id"
            )
        )
        self.first, self.second = steps[0], steps[1]
        self.first.duration_in_days = 5
        self.first.save(update_fields=["duration_in_days"])

    def proposal(self, call_round=None):
        return factories.ProposalFactory(round=call_round or self.round)

    def instance(self, proposal, call_step, status, days=None, outcome="", **kwargs):
        started = kwargs.pop("started_at", self.now - datetime.timedelta(days=10))
        values = {
            "proposal": proposal,
            "call_step": call_step,
            "step": call_step.key,
            "order": call_step.order,
            "status": status,
            "started_at": started,
            "outcome": outcome,
        }
        if status == COMPLETED:
            values["completed_at"] = started + datetime.timedelta(days=days)
        values.update(kwargs)
        return factories.ProposalWorkflowStepInstanceFactory(**values)

    def populate(self, completed=(2, 4), call_round=None):
        """Proposals at the first step: some completed in ``completed`` days,
        one waiting past its deadline and one within it."""
        for index, days in enumerate(completed):
            self.instance(
                self.proposal(call_round),
                self.first,
                COMPLETED,
                days=days,
                outcome="accepted" if index % 2 == 0 else "rejected",
                deadline=self.now,
            )
        self.instance(
            self.proposal(call_round),
            self.first,
            ACTIVE,
            deadline=self.now - datetime.timedelta(days=1),
        )
        self.instance(
            self.proposal(call_round),
            self.first,
            ACTIVE,
            deadline=self.now + datetime.timedelta(days=1),
        )


class ProgressTest(Base):
    def step(self, data, key):
        return next(s for s in data["steps"] if s["key"] == key)

    def test_counts_durations_and_outcome_shares(self):
        self.populate()
        data = workflow_observability.progress(self.call, now=self.now)
        first = self.step(data, self.first.key)
        self.assertEqual(data["proposals"], 4)
        self.assertEqual(first["planned_days"], 5)
        self.assertEqual(first["waiting"], 2)
        self.assertEqual(first["overdue"], 1)
        self.assertEqual(first["completed"], 2)
        self.assertEqual(first["median_days"], 3.0)
        self.assertEqual(first["p90_days"], 3.8)
        self.assertEqual(
            {o["outcome"]: o["share"] for o in first["outcomes"]},
            {"accepted": 0.5, "rejected": 0.5},
        )
        second = self.step(data, self.second.key)
        self.assertEqual(second["total"], 0)
        self.assertIsNone(second["median_days"])

    def test_late_skipped_steps_are_counted(self):
        proposal = self.proposal()
        self.instance(
            proposal,
            self.first,
            COMPLETED,
            days=3,
            outcome="accepted",
            deadline=self.now - datetime.timedelta(days=9),
        )
        self.instance(proposal, self.second, SKIPPED)
        data = workflow_observability.progress(self.call, now=self.now)
        self.assertEqual(self.step(data, self.first.key)["completed_late"], 1)
        self.assertEqual(self.step(data, self.second.key)["skipped"], 1)

    def test_query_count_does_not_grow_with_proposals(self):
        self.populate(completed=(1,))
        with CaptureQueriesContext(connection) as few:
            workflow_observability.progress(self.call)
        self.populate(completed=(1, 2, 3, 4, 5, 6))
        with CaptureQueriesContext(connection) as many:
            workflow_observability.progress(self.call)
        self.assertEqual(len(few), len(many))

    def test_a_round_step_is_counted_per_round(self):
        models.CallWorkflowStep.objects.filter(pk=self.second.pk).update(
            round_scoped=True
        )
        other = factories.RoundFactory(call=self.call)
        for call_round in (self.round, self.round, other):
            self.instance(self.proposal(call_round), self.second, ACTIVE)
        due = self.now - datetime.timedelta(days=1)
        models.RoundStep.objects.create(
            round=self.round, call_step=self.second, due_at=due, opened_at=self.now
        )
        models.RoundMeetingNote.objects.create(
            round=self.round,
            call_step=self.second,
            title="Report",
            author=structure_factories.UserFactory(),
            submitted_at=self.now,
        )
        data = workflow_observability.progress(self.call, now=self.now)
        second = self.step(data, self.second.key)
        self.assertTrue(second["round_scoped"])
        self.assertEqual(second["phase"], "evaluation")
        per_round = {r["round"]: r for r in second["rounds"]}
        self.assertEqual(
            per_round[self.round.uuid.hex],
            {
                "round": self.round.uuid.hex,
                "name": self.round.name,
                "opened": True,
                "due_at": due,
                "overdue": True,
                "waiting": 2,
                "completed": 0,
                "reports": 1,
                "reports_submitted": 1,
            },
        )
        self.assertEqual(per_round[other.uuid.hex]["waiting"], 1)
        self.assertFalse(per_round[other.uuid.hex]["opened"])
        self.assertEqual(self.step(data, self.first.key)["rounds"], [])
        with CaptureQueriesContext(connection) as few:
            workflow_observability.progress(self.call)
        factories.RoundFactory(call=self.call)
        with CaptureQueriesContext(connection) as many:
            workflow_observability.progress(self.call)
        self.assertEqual(len(few), len(many))

    def test_bounded_by_round(self):
        self.populate()
        other = factories.RoundFactory(call=self.call)
        self.populate(completed=(1,), call_round=other)
        data = workflow_observability.progress(self.call, other)
        self.assertEqual(data["round"], other.uuid.hex)
        self.assertEqual(data["proposals"], 3)
        self.assertEqual(self.step(data, self.first.key)["completed"], 1)
        self.assertEqual(len(data["rounds"]), 2)


class TimelineTest(Base):
    def test_steps_planned_against_actual_with_mail_in_order(self):
        proposal = self.proposal()
        completer = structure_factories.UserFactory(
            first_name="Ada", last_name="Lovelace"
        )
        first = self.instance(
            proposal,
            self.first,
            COMPLETED,
            days=7,
            outcome="accepted",
            completed_by=completer,
            deadline=self.now - datetime.timedelta(days=5),
        )
        self.instance(proposal, self.second, SKIPPED)
        activity.log_notification_sent(
            proposal,
            "workflow_step_event",
            NotificationRuleTriggers.STEP_STARTED,
            NotificationRuleRecipients.APPLICANT,
            2,
            step_instance=first,
        )
        activity.log_notification_sent(
            proposal, "proposal_state_changed", "accepted", "applicant", 1
        )
        data = workflow_observability.proposal_timeline(
            proposal, self.fixture.staff, now=self.now
        )
        self.assertEqual(
            [(s["key"], s["status"]) for s in data["steps"]],
            [(self.first.key, COMPLETED), (self.second.key, SKIPPED)],
        )
        step = data["steps"][0]
        self.assertTrue(step["overdue"])
        self.assertEqual(step["planned_days"], 5)
        self.assertEqual(step["completed_by_name"], completer.full_name)
        self.assertEqual(
            [
                (n["template"], n["step_key"], n["audience"], n["recipient_count"])
                for n in data["notifications"]
            ],
            [
                ("workflow_step_event", self.first.key, "applicant", 2),
                ("proposal_state_changed", None, "applicant", 1),
            ],
        )
        self.assertNotIn(completer.email, json.dumps(data, default=str))

    def test_held_decision_outcome_is_masked_for_those_not_seeing_it(self):
        proposal = self.proposal()
        instance = self.instance(
            proposal, self.first, COMPLETED, days=1, outcome="accepted"
        )
        instance.is_allocation_decision = True
        instance.save(update_fields=["is_allocation_decision"])
        proposal.decision_held = True
        proposal.save(update_fields=["decision_held"])
        user = structure_factories.UserFactory()
        with mock.patch.object(
            workflow_observability.proposal_permissions,
            "user_can_view_held_decision",
            return_value=False,
        ):
            data = workflow_observability.proposal_timeline(proposal, user)
        self.assertIsNone(data["steps"][0]["outcome"])


class PathsTest(Base):
    def walk(self, *steps):
        proposal = self.proposal()
        for call_step, step_status, outcome in steps:
            self.instance(proposal, call_step, step_status, days=1, outcome=outcome)
        return proposal

    def test_distinct_paths_with_counts(self):
        accepted = ((self.first, COMPLETED, "accepted"), (self.second, ACTIVE, ""))
        self.walk(*accepted)
        self.walk(*accepted)
        self.walk((self.first, COMPLETED, "rejected"))
        data = workflow_observability.paths(self.call)
        self.assertFalse(data["scenarios_known"])
        self.assertEqual(
            [
                (
                    [(s["key"], s["result"]) for s in p["steps"]],
                    p["proposals"],
                    p["finished"],
                    p["covered_by_scenario"],
                )
                for p in data["paths"]
            ],
            [
                (
                    [(self.first.key, "accepted"), (self.second.key, "active")],
                    2,
                    False,
                    None,
                ),
                ([(self.first.key, "rejected")], 1, True, None),
            ],
        )

    def test_paths_are_matched_against_saved_scenarios(self):
        self.walk((self.first, COMPLETED, "accepted"), (self.second, ACTIVE, ""))
        self.walk((self.first, COMPLETED, "rejected"))
        scenario = [
            (self.first.key, "accepted"),
            (self.second.key, "active"),
            ("allocation_decision", "accepted"),
        ]
        with mock.patch.object(
            workflow_observability,
            "SCENARIO_PATH_SOURCES",
            [lambda call: [scenario]],
        ):
            data = workflow_observability.paths(self.call)
        self.assertTrue(data["scenarios_known"])
        self.assertEqual(
            [p["covered_by_scenario"] for p in data["paths"]], [True, False]
        )


class EndpointsTest(Base):
    def setUp(self):
        super().setUp()
        self.populate()
        self.target = models.Proposal.objects.filter(round=self.round).first()

    def urls(self):
        return {
            "progress": factories.CallFactory.get_protected_url(
                self.call, "workflow-progress"
            ),
            "paths": factories.CallFactory.get_protected_url(
                self.call, "workflow-paths"
            ),
            "timeline": factories.ProposalFactory.get_url(
                self.target, "workflow-timeline"
            ),
        }

    def get(self, user, url):
        self.client.force_authenticate(user)
        return self.client.get(url)

    def test_call_team_staff_and_support_read_them(self):
        for user in (
            self.fixture.call_manager,
            self.fixture.call_organizer_user,
            self.fixture.staff,
            structure_factories.UserFactory(is_support=True),
        ):
            for name, url in self.urls().items():
                with self.subTest(user=user, endpoint=name):
                    self.assertEqual(
                        self.get(user, url).status_code, status.HTTP_200_OK
                    )

    def test_reviewers_panel_and_applicants_get_404(self):
        applicant = fixtures.author_as_manager(self.target)
        for user in (
            self.fixture.panel_member,
            self.fixture.reviewer_1,
            self.fixture.proposal_creator,
            applicant,
            structure_factories.UserFactory(),
        ):
            for name, url in self.urls().items():
                with self.subTest(user=user, endpoint=name):
                    self.assertEqual(
                        self.get(user, url).status_code, status.HTTP_404_NOT_FOUND
                    )

    def test_applicant_managing_the_call_does_not_read_their_proposals_timeline(self):
        applicant = fixtures.author_as_manager(self.target)
        self.call.add_user(applicant, CallRole.MANAGER)
        response = self.get(applicant, self.urls()["timeline"])
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.get(applicant, self.urls()["progress"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_round_parameter(self):
        url = self.urls()["progress"]
        response = self.get(self.fixture.staff, f"{url}?round={self.round.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["round"], self.round.uuid.hex)
        other = factories.RoundFactory()
        for value in (other.uuid.hex, "not-a-uuid"):
            with self.subTest(round=value):
                response = self.get(self.fixture.staff, f"{url}?round={value}")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class NotificationSentEventTest(Base):
    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.target = self.fixture.proposal
        self.applicant = self.fixture.proposal_creator
        self.step_instance = self.instance(self.target, self.first, ACTIVE)
        self.call_manager = self.fixture.call_manager
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.first,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
        )

    def events(self):
        return logging_models.Event.objects.filter(
            event_type=EventType.PROPOSAL_WORKFLOW_NOTIFICATION_SENT
        )

    def send(self):
        mail.outbox = []
        tasks.notify_workflow_step_event(
            self.step_instance.uuid.hex, NotificationRuleTriggers.STEP_STARTED
        )

    def test_a_sent_mail_logs_one_event_naming_no_address(self):
        self.send()
        self.assertEqual(len(mail.outbox), 1)
        [event] = self.events()
        context = event.context
        self.assertEqual(context["template"], "workflow_step_event")
        self.assertEqual(context["trigger"], NotificationRuleTriggers.STEP_STARTED)
        self.assertEqual(context["audience"], NotificationRuleRecipients.CALL_MANAGERS)
        self.assertEqual(context["recipient_count"], len(mail.outbox[0].to))
        self.assertEqual(context["step_key"], self.first.key)
        self.assertNotIn("user_uuid", context)
        text = event.message + json.dumps(context)
        for address in mail.outbox[0].to:
            self.assertNotIn(address, text)
        self.assertNotIn("@", text)

    def test_the_call_team_opens_the_mail_as_sent(self):
        self.send()
        self.client.force_authenticate(self.call_manager)
        timeline = self.client.get(
            factories.ProposalFactory.get_url(self.target, "workflow-timeline")
        ).data
        [row] = timeline["notifications"]
        self.assertTrue(row["email_log_uuids"])
        url = factories.ProposalFactory.get_url(self.target, "workflow-mails")
        response = self.client.get(url, {"uuid": row["email_log_uuids"]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        [sent] = response.data
        self.assertEqual(sent["subject"], mail.outbox[0].subject)
        self.assertEqual(sorted(sent["emails"]), sorted(mail.outbox[0].to))
        self.assertIn(self.target.name, sent["body"])

        # Never another proposal's mail, whatever is asked for.
        other = logging_models.EmailLog.objects.create(
            subject="Elsewhere", body="Not this proposal's.", emails=["a@b.c"]
        )
        response = self.client.get(url, {"uuid": [other.uuid.hex]})
        self.assertEqual(response.data, [])

        # Nor for the applicant team.
        self.client.force_authenticate(self.applicant)
        response = self.client.get(url, {"uuid": row["email_log_uuids"]})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_submission_lists_both_its_mails_as_sent(self):
        # The applicant is told, and so are the call's managers: both rows
        # on the timeline, both opened as sent.
        for key in (
            "proposal.proposal_state_changed",
            "proposal.new_proposal_submitted",
        ):
            Notification.objects.update_or_create(key=key, defaults={"enabled": True})
        self.target.state = ProposalStates.SUBMITTED
        self.target.save()
        mail.outbox = []
        tasks.notify_user_about_proposal_state_update(
            self.target.uuid.hex, ProposalStates.DRAFT, ProposalStates.SUBMITTED
        )
        tasks.notify_call_managers_about_new_proposal_submission(self.target.uuid.hex)
        self.assertEqual(len(mail.outbox), 2)
        self.client.force_authenticate(self.call_manager)
        timeline = self.client.get(
            factories.ProposalFactory.get_url(self.target, "workflow-timeline")
        ).data
        rows = {row["template"]: row for row in timeline["notifications"]}
        self.assertEqual(
            set(rows), {"proposal_state_changed", "new_proposal_submitted"}
        )
        sent = rows["new_proposal_submitted"]
        self.assertEqual(sent["audience"], NotificationRuleRecipients.CALL_MANAGERS)
        self.assertEqual(sent["trigger"], ProposalStates.SUBMITTED)
        self.assertTrue(sent["email_log_uuids"])
        self.assertTrue(rows["proposal_state_changed"]["email_log_uuids"])

    def test_a_failed_send_logs_nothing(self):
        with mock.patch(
            "waldur_core.core.utils.send_mail", side_effect=RuntimeError("down")
        ):
            self.send()
        self.assertFalse(self.events().exists())

    def test_a_disabled_notification_logs_nothing(self):
        Notification.objects.filter(key="proposal.workflow_step_event").update(
            enabled=False
        )
        self.send()
        self.assertFalse(self.events().exists())

    def test_the_applicant_team_does_not_read_it(self):
        self.send()
        rows = activity.proposal_event_rows(self.applicant, self.target)
        self.assertFalse(self.events().filter(rows).exists())


class ScheduleTest(test.APITestCase):
    def test_each_step_is_planned_from_its_stage(self):
        document = {
            "call": {"name": "Call"},
            "workflow_steps": [
                {"key": "a", "order": 0, "duration_in_days": 3},
                {"key": "b", "order": 1, "duration_in_days": 5},
                {
                    "key": "c",
                    "order": 2,
                    "duration_in_days": 8,
                    "parallel_with_previous": True,
                    "run_condition": {"step": "a", "outcomes": ["accepted"]},
                },
                {"key": "d", "order": 3, "duration_in_days": 2},
            ],
        }
        schedule = wd.describe(document)["schedule"]
        planned = {
            s["key"]: (s["start_day"], s["end_day"], s["conditional"], s["critical"])
            for s in schedule["steps"]
        }
        self.assertEqual(
            planned,
            {
                "a": (0, 3, False, True),
                "b": (3, 8, False, False),
                "c": (3, 11, True, True),
                "d": (11, 13, False, True),
            },
        )
        self.assertEqual(schedule["critical_path"], ["a", "c", "d"])
        self.assertEqual(schedule["total_days"], 13)


class DrawnStepFieldsTest(test.APITestCase):
    def test_later_step_fields_are_carried_only_when_present(self):
        document = {
            "call": {"name": "Call"},
            "workflow_steps": [
                {"key": "a", "order": 0},
                {
                    "key": "b",
                    "order": 1,
                    "round_scoped": True,
                    "status_labels": {"applicant": "Panel meeting"},
                },
            ],
        }
        steps = {s["key"]: s for s in wd.describe(document)["steps"]}
        self.assertEqual(steps["a"]["extra"], {"round_scoped": False})
        self.assertTrue(steps["b"]["extra"]["round_scoped"])
        self.assertEqual(
            steps["b"]["extra"]["status_labels"], {"applicant": "Panel meeting"}
        )
