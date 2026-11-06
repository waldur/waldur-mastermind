"""Dry run of a call's workflow on a synthetic proposal.

The call team replays a path through the call's workflow and reads what the
real engine makes of it, and every mail it would send. Nothing survives a
run: no row, no mail, no task, no cache entry, no request out.
"""

import json
import re
from datetime import timedelta
from unittest import mock

import psycopg
from django.core import mail
from django.core.cache import cache
from django.db import InternalError, connection, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.core import utils as core_utils
from waldur_core.core.models import User
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    call_templates,
    dry_run,
    models,
    tasks,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    ContributionOutcomes,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    RequestedOfferingStates,
)
from waldur_mastermind.proposal.tests import factories, fixtures

NEGATIVE = {"declined", "ineligible", "infeasible", "rejected"}

THROTTLE = "waldur_mastermind.proposal.views.WorkflowDryRunThrottle.allow_request"

# Every table a dry run writes to; none may hold a row more after it.
COUNTED = (
    models.Proposal,
    models.Round,
    models.ProposalWorkflowStepInstance,
    models.StepContribution,
    models.Review,
    models.ProposalRoleAssignment,
    models.ProposalChangeRequest,
    models.RequestedResource,
    models.AwardedResource,
    models.Call,
    User,
    UserRole,
    Event,
    Feed,
    checklist_models.ChecklistCompletion,
    checklist_models.Answer,
    structure_models.Customer,
    structure_models.Project,
    marketplace_models.Order,
    marketplace_models.Resource,
    models.RoundStep,
    models.RoundMeetingNote,
    models.ProposalRun,
    models.CallPanel,
    models.CallOfferingComponent,
    models.CallResourcePool,
)


def row_counts():
    return {model.__name__: model.objects.count() for model in COUNTED}


class DryRunMixin:
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.staff = structure_factories.UserFactory(is_staff=True)
        throttle = mock.patch(THROTTLE, return_value=True)
        throttle.start()
        self.addCleanup(throttle.stop)

    def import_template(self, name, offerings=2):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {
                "manager": self.fixture.manager.uuid.hex,
                "call_data": call_templates.load(name),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.offering_managers = []
        for number in range(offerings):
            offering = marketplace_factories.OfferingFactory(name=f"Offering {number}")
            manager = structure_factories.UserFactory(
                first_name="Olivia", last_name=f"Realmanager{number}"
            )
            offering.add_user(manager, OfferingRole.MANAGER)
            self.offering_managers.append(manager)
            factories.RequestedOfferingFactory(
                call=call,
                state=RequestedOfferingStates.ACCEPTED,
                offering=offering,
                created_by=self.fixture.owner,
            )
        return call

    def scenario_call(self, name, offerings=1):
        """A call created from the template ``name`` with the people a
        scenario reads mail for: a call manager, and a panel chair and
        vice-chair on the panel."""
        call = self.import_template(name, offerings=offerings)
        call.add_user(structure_factories.UserFactory(), RoleEnum.CALL_MANAGER)
        chairs = [structure_factories.UserFactory() for _ in range(2)]
        for chair in chairs:
            call.add_user(chair, RoleEnum.CALL_PANEL_MEMBER)
        models.Call.objects.filter(pk=call.pk).update(
            panel_chair=chairs[0], panel_vice_chair=chairs[1]
        )
        call.refresh_from_db()
        return call

    def dry(self, call, actions=(), user=None, **extra):
        self.client.force_authenticate(user or self.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(call, "workflow-dry-run"),
            {"actions": list(actions), **extra},
            format="json",
        )

    def ok(self, call, actions=(), **extra):
        response = self.dry(call, actions, **extra)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def walk(self, call, stop=None, **extra):
        """The happy path: every step's first positive outcome, a manual
        advance or a publication when one is due, until the proposal is
        decided (or ``stop`` says so)."""
        actions = []
        for _ in range(60):
            data = self.ok(call, actions, **extra)
            if data["proposal_state"] != ProposalStates.IN_REVIEW:
                return actions, data
            if stop and stop(data):
                return actions, data
            nxt = data["next"]
            if nxt["can_publish"]:
                actions.append({"publish": True})
                continue
            if nxt["can_advance"]:
                actions.append({"advance": True})
                continue
            step = next(s for s in nxt["steps"] if s["outcomes"])
            outcome = next(o for o in step["outcomes"] if o not in NEGATIVE)
            action = {"step": step["step"], "outcome": outcome}
            if step["round_step"]:
                action["round_step"] = True
            actions.append(action)
        self.fail("The path did not end.")

    @staticmethod
    def step(data, key):
        return next(s for s in data["steps"] if s["key"] == key)

    @staticmethod
    def kinds(data):
        return [entry["kind"] for entry in data["log"]]


class PermissionTest(DryRunMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call = self.fixture.call
        self.fixture.requested_offering_accepted

    def test_call_team_and_staff_run_it(self):
        for user in (
            self.fixture.call_manager,
            self.fixture.call_organizer_user,
            self.staff,
        ):
            with self.subTest(user=user):
                self.assertEqual(
                    self.dry(self.call, user=user).status_code, status.HTTP_200_OK
                )

    def test_support_panel_reviewers_and_applicants_do_not(self):
        for user in (
            structure_factories.UserFactory(is_support=True),
            self.fixture.panel_member,
            self.fixture.reviewer_1,
            self.fixture.proposal_creator,
            structure_factories.UserFactory(),
        ):
            with self.subTest(user=user):
                self.assertIn(
                    self.dry(self.call, user=user).status_code,
                    (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                )

    def test_support_is_refused_although_it_sees_the_call(self):
        support = structure_factories.UserFactory(is_support=True)
        self.client.force_authenticate(support)
        self.assertEqual(
            self.client.get(
                factories.CallFactory.get_protected_url(self.call)
            ).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.dry(self.call, user=support).status_code, status.HTTP_403_FORBIDDEN
        )


class TemplatePathTest(DryRunMixin, test.APITestCase):
    def test_every_template_walks_to_an_award(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                call = self.import_template(name)
                _, data = self.walk(call)
                self.assertEqual(data["proposal_state"], ProposalStates.ACCEPTED)
                self.assertIn("allocation", self.kinds(data))
                self.assertIn("decided", self.kinds(data))
                self.assertTrue(data["messages"])

    def test_replaying_a_prefix_is_stopping_there(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                call = self.import_template(name)
                actions, full = self.walk(call)

                def seen(data, upto):
                    return (
                        [
                            {k: v for k, v in entry.items() if k != "index"}
                            for entry in data["log"]
                            if entry["action"] is None or entry["action"] < upto
                        ],
                        [
                            (m["subject"], m["audience"], m["day"])
                            for m in data["messages"]
                            if m["action"] is None or m["action"] < upto
                        ],
                    )

                for upto in range(len(actions)):
                    prefix = self.ok(call, actions[:upto])
                    self.assertEqual(seen(prefix, upto), seen(full, upto))
                    # Each request replays from scratch: the same path, the
                    # same result.
                    again = self.ok(call, actions[:upto])
                    self.assertEqual(again["steps"], prefix["steps"])

    def test_view_as_at_stops_the_replay(self):
        call = self.import_template("ai_factory_large_scale")
        actions, _ = self.walk(call)
        stopped = self.ok(call, actions, view_as={"role": "applicant", "at": 1})
        prefix = self.ok(call, actions[:1])
        self.assertEqual(stopped["steps"], prefix["steps"])


class EngineTest(DryRunMixin, test.APITestCase):
    def test_conditional_step_is_skipped_and_runs_when_its_condition_holds(self):
        call = self.import_template("quantum_access")
        accepted = self.ok(
            call,
            [
                {
                    "step": "technical_assessment",
                    "contributions": [
                        {"offering": 0, "outcome": "accepted"},
                        {"offering": 1, "outcome": "accepted"},
                    ],
                },
                # The call managers move a technical assessment on.
                {"advance": True},
            ],
        )
        emulation = self.step(accepted, "emulation_assessment")
        self.assertEqual(emulation["status"], "skipped")
        self.assertEqual(emulation["skipped_reason"], "condition")
        self.assertIn("skipped", self.kinds(accepted))

        conditional = self.ok(
            call,
            [
                {
                    "step": "technical_assessment",
                    "contributions": [
                        {"offering": 0, "outcome": "conditionally_accepted"},
                        {"offering": 1, "outcome": "accepted"},
                    ],
                },
                {"advance": True},
            ],
        )
        emulation = self.step(conditional, "emulation_assessment")
        self.assertEqual(emulation["status"], "active")
        # Only the offering accepted on conditions assesses again.
        self.assertEqual([c["offering"] for c in emulation["contributions"]], [0])

    def test_automatic_decision_awards_at_once(self):
        call = self.import_template("ai_factory_fast_lane")
        data = self.ok(
            call,
            [
                {
                    "step": "technical_assessment",
                    "contributions": [
                        {"offering": 0, "outcome": "accepted"},
                        {"offering": 1, "outcome": "accepted"},
                    ],
                }
            ],
        )
        self.assertEqual(data["proposal_state"], ProposalStates.ACCEPTED)
        self.assertTrue(self.step(data, "allocation_decision")["automatic"])
        self.assertIn("automatic_decision", self.kinds(data))
        decided = [
            m
            for m in data["messages"]
            if m["notification"] == "proposal.proposal_state_changed"
            and m["action"] == 0
        ]
        self.assertEqual(len(decided), 1)
        self.assertEqual(decided[0]["audience"]["actor"], "applicant")

    def test_rejection_by_contributions_ends_the_workflow(self):
        call = self.import_template("ai_factory_fast_lane")
        data = self.ok(
            call, [{"step": "technical_assessment", "outcome": "infeasible"}]
        )
        self.assertEqual(data["proposal_state"], ProposalStates.REJECTED)
        assessment = self.step(data, "technical_assessment")
        self.assertEqual(assessment["outcome"], "infeasible")
        self.assertIn(
            ContributionOutcomes.REJECTED,
            [c["outcome"] for c in assessment["contributions"]],
        )

    def test_parallel_stage_starts_together_joins_and_withdraws(self):
        call = self.import_template("ai_factory_large_scale")
        eligible = [{"step": "administrative_check", "outcome": "eligible"}]
        data = self.ok(call, eligible)
        running = {s["key"] for s in data["steps"] if s["status"] == "active"}
        self.assertEqual(
            running, {"technical_assessment", "innovation_group_evaluation"}
        )
        self.assertEqual(
            self.step(data, "technical_assessment")["stage"],
            self.step(data, "innovation_group_evaluation")["stage"],
        )

        one = self.ok(
            call, eligible + [{"step": "technical_assessment", "outcome": "feasible"}]
        )
        # The next stage waits for the other step of the stage.
        self.assertEqual(self.step(one, "ranking_list_adoption")["status"], "pending")
        both = self.ok(
            call,
            eligible
            + [
                {"step": "technical_assessment", "outcome": "feasible"},
                {"step": "innovation_group_evaluation", "outcome": "feasible"},
                # Both move on manually, together.
                {"advance": True},
            ],
        )
        self.assertEqual(self.step(both, "ranking_list_adoption")["status"], "active")

        rejected = self.ok(
            call,
            eligible + [{"step": "technical_assessment", "outcome": "infeasible"}],
        )
        self.assertEqual(rejected["proposal_state"], ProposalStates.REJECTED)
        self.assertEqual(
            self.step(rejected, "innovation_group_evaluation")["status"], "withdrawn"
        )
        self.assertIn("withdrawn", self.kinds(rejected))

    def test_changes_requested_and_submitted(self):
        call = self.import_template("ai_factory_large_scale")
        # The changes submitted are told to the step's actor.
        call.add_user(self.fixture.call_manager, "CALL.MANAGER")
        requested = self.ok(
            call, [{"step": "administrative_check", "request_changes": True}]
        )
        self.assertTrue(
            self.step(requested, "administrative_check")["changes_requested"]
        )
        self.assertTrue(requested["next"]["can_submit_changes"])
        told = [m for m in requested["messages"] if m["action"] == 0]
        self.assertIn("applicant", [m["audience"]["actor"] for m in told])
        # The step waits on the applicant: it cannot be decided meanwhile.
        refused = self.dry(
            call,
            [
                {"step": "administrative_check", "request_changes": True},
                {"step": "administrative_check", "outcome": "eligible"},
            ],
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(refused.data["action"], 1)
        self.assertEqual(refused.data["step"], "administrative_check")

        submitted = self.ok(
            call,
            [
                {"step": "administrative_check", "request_changes": True},
                {"submit_changes": True},
                {"step": "administrative_check", "outcome": "eligible"},
            ],
        )
        self.assertEqual(
            self.step(submitted, "administrative_check")["outcome"], "eligible"
        )
        self.assertIn("changes_submitted", self.kinds(submitted))
        self.assertTrue([m for m in submitted["messages"] if m["action"] == 1])

    def test_delegated_work_is_written_by_the_delegate(self):
        call = self.import_template("ai_factory_fast_lane")
        data = self.ok(
            call,
            [
                {"step": "technical_assessment", "delegate": {"offering": 0}},
                {
                    "step": "technical_assessment",
                    "contributions": [{"offering": 0, "outcome": "accepted"}],
                },
            ],
        )
        slot = self.step(data, "technical_assessment")["contributions"][0]
        self.assertEqual(slot["status"], "submitted")
        self.assertIn("delegated", self.kinds(data))
        self.assertTrue(
            [
                m
                for m in data["messages"]
                if m["notification"] == "proposal.contribution_delegated"
            ]
        )

    def test_lapse_and_reminders_come_at_the_deadline(self):
        call = self.import_template("ai_factory_large_scale")
        assessment = models.CallWorkflowStep.objects.get(
            call=call, key="administrative_check"
        )
        models.CallWorkflowStepNotificationRule.objects.get_or_create(
            workflow_step=assessment,
            trigger=NotificationRuleTriggers.DEADLINE_APPROACHING,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
            days_before=1,
        )
        call.add_user(self.fixture.call_manager, "CALL.MANAGER")
        data = self.ok(call, [{"step": "administrative_check", "expire": True}])
        check = self.step(data, "administrative_check")
        self.assertEqual(check["status"], "expired")
        self.assertEqual(check["ended_day"], check["deadline_day"])
        reminders = [
            m
            for m in data["messages"]
            if m["trigger"] == NotificationRuleTriggers.DEADLINE_APPROACHING
        ]
        self.assertTrue(reminders)
        self.assertEqual(reminders[0]["day"], check["deadline_day"] - 1)

    def test_contribution_reopened(self):
        call = self.import_template("ai_factory_large_scale")
        data = self.ok(
            call,
            [
                {"step": "administrative_check", "outcome": "eligible"},
                {
                    "step": "technical_assessment",
                    "contributions": [{"offering": 0, "outcome": "accepted"}],
                },
                {"step": "technical_assessment", "reopen": {"offering": 0}},
            ],
        )
        slot = self.step(data, "technical_assessment")["contributions"][0]
        self.assertIn(slot["status"], ("pending", "accepted"))
        self.assertIn("contribution_reopened", self.kinds(data))

    def test_an_action_the_state_does_not_allow_names_the_step(self):
        call = self.import_template("ai_factory_large_scale")
        response = self.dry(
            call, [{"step": "allocation_decision", "outcome": "approved"}]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["step"], "allocation_decision")
        self.assertEqual(response.data["action"], 0)
        response = self.dry(call, [{"step": "nonexistent", "outcome": "done"}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.dry(
            call, [{"step": "administrative_check", "outcome": "approved"}]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_coverage_says_what_was_tried(self):
        call = self.import_template("ai_factory_fast_lane")
        data = self.ok(
            call, [{"step": "technical_assessment", "outcome": "infeasible"}]
        )
        coverage = data["coverage"]
        self.assertEqual(
            coverage["outcomes"]["technical_assessment"]["tried"], ["infeasible"]
        )
        self.assertIn("technical_assessment", coverage["exercised"])
        self.assertNotIn("allocation_decision", coverage["exercised"])


class MailTest(DryRunMixin, test.APITestCase):
    """The dry run's mail is the mail the same path sends for real."""

    def test_same_subjects_and_audiences_as_the_real_path(self):
        call = self.import_template("ai_factory_fast_lane")
        dry = self.ok(
            call,
            [
                {
                    "step": "technical_assessment",
                    "contributions": [
                        {"offering": 0, "outcome": "accepted"},
                        {"offering": 1, "outcome": "accepted"},
                    ],
                }
            ],
        )

        # The same path for real: a proposal requesting the same offerings,
        # assessed by their real teams. A real submission needs the call to
        # be active; the imported call is still a draft.
        call.state = CallStates.ACTIVE
        call.save(update_fields=["state"])
        proposal = factories.ProposalFactory(
            round=factories.RoundFactory(
                call=call,
                start_time=timezone.now() - timedelta(days=1),
                cutoff_time=timezone.now() + timedelta(days=10),
            ),
            state=ProposalStates.DRAFT,
            project=None,
            name="Example proposal",
        )
        fixtures.author_as_manager(proposal)
        requested = list(
            models.RequestedOffering.objects.filter(call=call).order_by("created", "id")
        )
        for requested_offering in requested:
            factories.RequestedResourceFactory(
                proposal=proposal, requested_offering=requested_offering
            )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.client.force_authenticate(proposal.created_by)
            response = self.client.post(
                factories.ProposalFactory.get_url(proposal, action="submit")
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        instance = proposal.workflow_step_instances.get(step="technical_assessment")
        for manager, requested_offering in zip(self.offering_managers, requested):
            slot = instance.contributions.get(requested_offering=requested_offering)
            with self.captureOnCommitCallbacks(execute=True):
                self.client.force_authenticate(manager)
                response = self.client.post(
                    factories.ProposalFactory.get_url(
                        proposal, action="submit_contribution"
                    ),
                    {"contribution_uuid": slot.uuid.hex, "outcome": "accepted"},
                    format="json",
                )
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.ACCEPTED)

        real = sorted(message.subject for message in mail.outbox)
        rendered = sorted(
            m["subject"]
            for m in dry["messages"]
            if m["enabled"]
            for _ in range(1 if m["single_message"] else m["audience"]["count"])
        )
        self.assertEqual(rendered, real)


class SideEffectTest(DryRunMixin, test.APITestCase):
    def test_nothing_survives_a_run(self):
        call = self.import_template("extreme_scale")
        actions, _ = self.walk(call)
        before = row_counts()
        mail.outbox = []
        with mock.patch("celery.app.task.Task.apply_async") as apply_async:
            with self.captureOnCommitCallbacks() as callbacks:
                data = self.ok(
                    call,
                    actions,
                    view_as={"role": "proposal_role_lead:rapporteur"},
                )
        self.assertTrue(data["messages"])
        self.assertEqual(row_counts(), before)
        self.assertEqual(mail.outbox, [])
        apply_async.assert_not_called()
        self.assertEqual(callbacks, [])

    def test_a_side_effect_reached_fails_the_run_and_leaves_nothing(self):
        call = self.import_template("ai_factory_fast_lane")
        on_commit_ran = []
        effects = {
            "mail": lambda: core_utils.send_mail("Hi", "Body", ["a@example.com"]),
            "broadcast": lambda: core_utils.broadcast_mail(
                "proposal", "workflow_step_event", {}, ["a@example.com"]
            ),
            "celery": lambda: tasks.notify_workflow_step_event.delay("x", "y"),
            "on_commit": lambda: transaction.on_commit(
                lambda: on_commit_ran.append(True)
            ),
            "cache": lambda: cache.set("dry-run-test", 1),
            "http": lambda: __import__("requests").get("http://example.invalid/"),
            "storage": lambda: __import__(
                "django.core.files.storage", fromlist=["default_storage"]
            ).default_storage.save(
                "dry-run.txt",
                __import__(
                    "django.core.files.base", fromlist=["ContentFile"]
                ).ContentFile(b"x"),
            ),
            "order": lambda: marketplace_factories.OrderFactory(),
        }
        original = dry_run._Run.log_contributions
        for name, effect in effects.items():
            with self.subTest(effect=name):

                def reach(run, instance, effect=effect):
                    original(run, instance)
                    effect()

                before = row_counts()
                mail.outbox = []
                with (
                    mock.patch.object(dry_run._Run, "log_contributions", reach),
                    mock.patch("requests.adapters.HTTPAdapter.send") as http,
                    self.captureOnCommitCallbacks(execute=True),
                ):
                    response = self.dry(call)
                self.assertEqual(
                    response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                self.assertEqual(
                    response.data,
                    {"detail": "The dry run failed. Nothing was sent or saved."},
                )
                self.assertEqual(row_counts(), before)
                self.assertEqual(mail.outbox, [])
                self.assertEqual(on_commit_ran, [])
                self.assertIsNone(cache.get("dry-run-test"))
                http.assert_not_called()

    def test_tripwires_are_gone_after_the_run(self):
        call = self.import_template("ai_factory_fast_lane")
        self.ok(call)
        mail.outbox = []
        core_utils.send_mail("Hi", "Body", ["a@example.com"])
        self.assertEqual(len(mail.outbox), 1)
        cache.set("after-dry-run", 1)
        self.assertEqual(cache.get("after-dry-run"), 1)

    def test_tripwires_hold_only_where_a_run_is_armed(self):
        with dry_run.tripwires():
            token = dry_run._armed.set(False)
            try:
                cache.set("unarmed", 1)
            finally:
                dry_run._armed.reset(token)
            with self.assertRaises(dry_run.DryRunSideEffect):
                cache.set("armed", 1)
        self.assertEqual(cache.get("unarmed"), 1)
        self.assertIsNone(cache.get("armed"))


@override_settings(DEBUG=True)
class ErrorTest(DryRunMixin, test.APITestCase):
    def test_a_fault_tells_no_trace(self):
        call = self.import_template("ai_factory_fast_lane")
        with mock.patch.object(
            dry_run._Run, "setup", side_effect=KeyError("internal detail")
        ):
            response = self.dry(call)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertNotIn("internal detail", json.dumps(response.data))
        self.assertNotIn("Traceback", response.content.decode())


class ExposureTest(DryRunMixin, test.APITestCase):
    def test_no_real_name_or_address_is_returned(self):
        call = self.import_template("extreme_scale")
        managers = [
            structure_factories.UserFactory(
                first_name="Rita",
                last_name="Realcallmanager",
                email="rita@real.example",
            ),
            structure_factories.UserFactory(
                first_name="Paul", last_name="Realpanelchair", email="paul@real.example"
            ),
        ]
        for user in managers:
            call.add_user(user, "CALL.MANAGER")
        actions, data = self.walk(call)
        payloads = [json.dumps(data)]
        for role in ("applicant", "call_manager", "offering_team", "reviewer"):
            payloads.append(
                json.dumps(self.ok(call, actions, view_as={"role": role}), default=str)
            )
        people = managers + self.offering_managers + [self.staff]
        for payload in payloads:
            for user in people:
                for value in (user.email, user.username, user.last_name):
                    self.assertNotIn(value, payload)
        # Real recipients are counted.
        counted = [
            m["audience"]["count"]
            for m in data["messages"]
            if m["audience"]["actor"] == "call_manager"
        ]
        self.assertTrue(counted)
        self.assertGreaterEqual(max(counted), 2)


class LimitsTest(DryRunMixin, test.APITestCase):
    def test_busy_call_is_answered_429(self):
        call = self.import_template("ai_factory_fast_lane")
        params = connection.get_connection_params()
        params.pop("cursor_factory", None)
        params.pop("context", None)
        with psycopg.connect(**params, autocommit=True) as other:
            other.execute(
                "SELECT pg_advisory_lock(%s, %s)",
                [dry_run.ADVISORY_LOCK_NAMESPACE, call.pk],
            )
            response = self.dry(call)
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(self.dry(call).status_code, status.HTTP_200_OK)

    def test_throttle(self):
        call = self.import_template("ai_factory_fast_lane")
        mock.patch.stopall()
        cache.clear()
        with mock.patch(
            "waldur_mastermind.proposal.views.WorkflowDryRunThrottle.get_rate",
            return_value="2/min",
        ):
            codes = [self.dry(call).status_code for _ in range(3)]
        self.assertEqual(codes[:2], [status.HTTP_200_OK, status.HTTP_200_OK])
        self.assertEqual(codes[2], status.HTTP_429_TOO_MANY_REQUESTS)

    def test_a_run_over_its_budget_is_stopped(self):
        call = self.import_template("ai_factory_fast_lane")
        with mock.patch.object(dry_run, "TIMEOUT_SECONDS", 0):
            response = self.dry(
                call, [{"step": "technical_assessment", "outcome": "feasible"}]
            )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_a_run_the_database_ends_for_idling_is_over_its_budget(self):
        # The database's idle and transaction timeouts end the session, which
        # arrives as an InternalError: still the run's budget, not a fault.
        call = self.import_template("ai_factory_fast_lane")
        stopped = InternalError(
            "terminating connection due to idle-in-transaction timeout"
        )
        with mock.patch.object(dry_run._Run, "execute", side_effect=stopped):
            response = self.dry(call)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_request_size_is_capped(self):
        call = self.import_template("ai_factory_fast_lane")
        response = self.dry(call, [{"advance": True}] * 201)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.dry(call, requested_offerings=[f"{n:032x}" for n in range(11)])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.dry(
            call,
            [{"step": "technical_assessment", "reason": "x" * 1001, "reject": True}],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.dry(call, [{"step": "x", "reject": True, "expire": True}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offerings_must_be_accepted_by_the_call(self):
        call = self.import_template("ai_factory_fast_lane")
        other = factories.RequestedOfferingFactory(
            state=RequestedOfferingStates.ACCEPTED, created_by=self.fixture.owner
        )
        response = self.dry(call, requested_offerings=[other.uuid.hex])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ViewAsTest(DryRunMixin, test.APITestCase):
    def at_response_step(self, call):
        return self.walk(
            call,
            stop=lambda data: (
                self.step(data, "response_to_reviews")["status"] == "active"
            ),
        )

    def test_only_roles_of_the_description(self):
        call = self.import_template("extreme_scale")
        for role in (
            "system",
            "nobody",
            self.fixture.call_manager.uuid.hex,
            "proposal_role:x",
        ):
            with self.subTest(role=role):
                response = self.dry(call, view_as={"role": role})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.dry(
            call, view_as={"user": self.staff.uuid.hex, "role": "applicant"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["view_as"]["viewer"]["full_name"], "Example applicant"
        )

    def test_every_actor_previews(self):
        call = self.import_template("extreme_scale")
        actions, _ = self.at_response_step(call)
        description = self.client.get(
            factories.CallFactory.get_protected_url(call, "workflow-description")
        ).data
        for actor in description["actors"]:
            if actor["key"] == "system":
                continue
            with self.subTest(actor=actor["key"]):
                data = self.ok(call, actions, view_as={"role": actor["key"]})
                view = data["view_as"]
                self.assertEqual(view["role"], actor["key"])
                self.assertTrue(view["viewer"]["full_name"].startswith("Example"))
                self.assertIn("workflow_states", view["responses"])

    def test_reads_each_step_query_of_the_page(self):
        """The proposal page asks per step for the inputs of a started step
        that reads earlier steps or that the applicant responds through,
        for the form answers of a begun step with a form, and for the
        applicant's own form on a response step: the preview reads each."""
        call = self.import_template("extreme_scale")
        actions, _ = self.at_response_step(call)
        for role in ("applicant", "call_manager", "proposal_role:rapporteur"):
            with self.subTest(role=role):
                view = self.ok(call, actions, view_as={"role": role})["view_as"]
                responses = view["responses"]
                states = {
                    row["call_step_uuid"]: row
                    for row in responses["workflow_states"]["data"]
                }
                expected = set()
                for step_uuid, row in states.items():
                    started = row["status"] in ("active", "completed", "expired")
                    responds = (
                        row["kind"] == "applicant_action"
                        and row["outcome_set"] != "acceptance"
                    )
                    form = bool(row["checklist_status"])
                    if started and (responds or row["input_step_uuids"]):
                        expected.add(f"step_inputs:{step_uuid}")
                    if form and row["status"] not in ("pending", "skipped"):
                        expected.add(f"step_answers:{step_uuid}")
                    if form and started and responds:
                        expected.add(f"step_checklist:{step_uuid}")
                read = {key for key in responses if ":" in key}
                self.assertEqual(read, expected)
                response_step = next(
                    uuid
                    for uuid, row in states.items()
                    if row["step"] == "response_to_reviews"
                )
                self.assertIn(f"step_inputs:{response_step}", read)
                if role == "applicant":
                    self.assertEqual(
                        responses[f"step_inputs:{response_step}"]["status"], 200
                    )

    def reviewer_names_read_by_applicant(self, call):
        actions, data = self.at_response_step(call)
        view = self.ok(call, actions, view_as={"role": "applicant"})["view_as"]
        # Keyed by the step of the call's copy the run works on.
        step_uuid = next(
            row["call_step_uuid"]
            for row in view["responses"]["workflow_states"]["data"]
            if row["step"] == "response_to_reviews"
        )
        inputs = view["responses"][f"step_inputs:{step_uuid}"]
        self.assertEqual(inputs["status"], status.HTTP_200_OK, inputs)
        reviews = [r for output in inputs["data"] for r in output["reviews"]]
        self.assertTrue(reviews)
        return {r.get("reviewer_full_name") for r in reviews} - {None, ""}

    def test_applicant_reads_no_reviewer_name_while_identity_is_hidden(self):
        call = self.import_template("extreme_scale")
        call.reviewer_identity_visible_to_submitters = False
        call.save(update_fields=["reviewer_identity_visible_to_submitters"])
        self.assertEqual(self.reviewer_names_read_by_applicant(call), set())

    def test_applicant_reads_reviewer_names_once_identity_is_shown(self):
        call = self.import_template("extreme_scale")
        call.reviewer_identity_visible_to_submitters = True
        call.save(update_fields=["reviewer_identity_visible_to_submitters"])
        models.CallWorkflowStep.objects.filter(call=call).update(blind_review=False)
        names = self.reviewer_names_read_by_applicant(call)
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("Example reviewer") for name in names))

    def test_hidden_steps_are_internal_checks_for_the_applicant(self):
        call = self.import_template("extreme_scale")
        actions, _ = self.at_response_step(call)
        for role, hides in (("applicant", True), ("call_manager", False)):
            view = self.ok(call, actions, view_as={"role": role})["view_as"]
            states = view["responses"]["workflow_states"]["data"]
            names = {row["step"]: row for row in states}
            self.assertEqual(
                names["technical_assessment"].get("outcome") is None, hides, role
            )

    def test_offering_team_reads_its_own_offering(self):
        call = self.import_template("ai_factory_large_scale")
        view = self.ok(
            call,
            [{"step": "administrative_check", "outcome": "eligible"}],
            view_as={"role": "offering_team", "offering": 1},
        )["view_as"]
        self.assertEqual(view["responses"]["proposal"]["status"], status.HTTP_200_OK)
        contributions = view["responses"]["contributions"]["data"]
        offerings = {c.get("offering_name") for c in contributions} - {None}
        self.assertEqual(offerings, {"Offering 1"})

    def test_preview_changes_nothing(self):
        call = self.import_template("ai_factory_large_scale")
        before = row_counts()
        role_rows = UserRole.objects.count()
        self.ok(call, view_as={"role": "panel_chair"})
        self.ok(call, view_as={"role": "offering_team"})
        self.assertEqual(row_counts(), before)
        self.assertEqual(UserRole.objects.count(), role_rows)
        call.refresh_from_db()
        self.assertIsNone(call.panel_chair)


class WorkflowServiceHookTest(test.APITestCase):
    """Outside a dry run the engine provisions and queues as before."""

    def test_allocation_outside_a_dry_run_provisions(self):
        with mock.patch.object(workflow_service.utils, "allocate_proposal") as allocate:
            fixture = fixtures.ProposalFixture()
            proposal = factories.ProposalFactory(
                round=fixture.round, state=ProposalStates.IN_REVIEW, project=None
            )
            workflow_service._advance_to_next(proposal, None)
        allocate.assert_called_once()


class RealCallRowsTest(DryRunMixin, test.APITransactionTestCase):
    """A run neither writes nor locks a row of the real call or its rounds:
    another connection takes them with NOWAIT while it is in progress.

    APITransactionTestCase required: a second connection probes the rows.
    It sees committed rows only: in a test wrapped in a transaction the call
    and its rounds would not exist for it, and the probe would prove nothing.
    """

    def probe(self):
        params = connection.get_connection_params()
        params.pop("cursor_factory", None)
        params.pop("context", None)
        with psycopg.connect(**params) as other:
            for model, pks in (
                (models.Call, [self.call.pk]),
                (models.Round, list(self.call.round_set.values_list("pk", flat=True))),
            ):
                for pk in pks:
                    rows = other.execute(
                        f"SELECT id FROM {model._meta.db_table} "
                        "WHERE id = %s FOR UPDATE NOWAIT",
                        [pk],
                    ).fetchall()
                    self.probed.append((model.__name__, rows))
            other.rollback()

    # What is tested is that the real rows stay free, not the budget: the
    # longest runs of this walk come close to it on a loaded runner.
    @mock.patch.object(dry_run, "TIMEOUT_SECONDS", 120)
    def test_real_call_and_rounds_stay_free(self):
        self.call = self.import_template("extreme_scale")
        factories.RoundFactory(call=self.call)
        self.probed = []
        actions, _ = self.walk(self.call)
        before = row_counts()
        original_settle = dry_run._Run.settle
        original_read = dry_run._read

        def settle(run):
            original_settle(run)
            self.probe()

        def read(*args, **kwargs):
            self.probe()
            return original_read(*args, **kwargs)

        with (
            mock.patch.object(dry_run._Run, "settle", settle),
            mock.patch.object(dry_run, "_read", read),
        ):
            for role in ("panel_chair", "panel_member", "call_manager"):
                self.ok(self.call, actions, view_as={"role": role})
        self.assertTrue(self.probed)
        # Every probe found its row and took it at once.
        self.assertTrue(all(rows for _, rows in self.probed), self.probed)
        self.assertEqual(row_counts(), before)
        self.call.refresh_from_db()
        self.assertIsNone(self.call.panel_chair)


UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")
TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"
)


def _sorted_lists(value):
    if isinstance(value, dict):
        return {key: _sorted_lists(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [_sorted_lists(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def normalised(value):
    """``value`` as two runs of the same path agree on it.

    Uuids, timestamps, the database ids of the call's copy and of a
    synthetic person, and the random part of their username differ from run
    to run. So do the
    order of a blind step's work and its anonymous labels: numbered in
    ``contributions.ANONYMOUS_ORDER``, random on purpose, so a place in the
    list tells nothing. Lists are therefore compared as multisets.
    """
    text = json.dumps(value, sort_keys=True, default=str)
    text = UUID.sub("<uuid>", text)
    text = TIMESTAMP.sub("<ts>", text)
    text = re.sub(r"dry-run-[0-9a-z]+(-\d+)?", "dry-run-<slug>", text)
    text = re.sub(r"proposal_role:\d+", "proposal_role:<id>", text)
    # A form answer names who gave it by id.
    text = re.sub(r'"user": \d+', '"user": "<id>"', text)
    text = re.sub(r"(Reviewer|Contributor) \d+", r"\1 #", text)
    data = json.loads(text)
    if isinstance(data.get("data"), dict):
        # Where the request came from: the test client's, or none.
        data["data"].pop("ip_address", None)
    return _sorted_lists(data)


def by_step(responses):
    """``responses`` with each read of a step -- its inputs, its form's
    answers, the applicant's form -- keyed by the step's key: the call step
    uuids differ between two runs."""
    states = responses["workflow_states"]
    keys = (
        {row["call_step_uuid"]: row["step"] for row in states["data"]}
        if states["status"] == 200
        else {}
    )

    def step_key(key):
        kind, _, step_uuid = key.partition(":")
        return f"{kind}:{keys.get(step_uuid, '?')}" if step_uuid else key

    return {step_key(key): value for key, value in responses.items()}


class ViewAsEqualityTest(DryRunMixin, test.APITestCase):
    maxDiff = None
    """What a preview shows is what a real holder of the role reads.

    The real proposal is the same path replayed and kept -- with the same
    synthetic holder given the role for real -- and read through the API as
    that holder; the preview comes from the endpoint, rolled back."""

    ORIGIN = {
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "HTTP_HOST": "testserver",
    }

    def real_reads(self, call, actions, role):
        the_run, _ = dry_run.replay(
            call,
            {"actions": actions, "view_as": {"role": role}},
            self.ORIGIN,
            deadline=float("inf"),
        )
        self.client.force_authenticate(the_run.viewer)
        reads = {}
        for key, (path, query) in the_run.reads.items():
            if key == "events":
                query = {"scope": f"http://testserver{query['scope']}"}
            response = self.client.get(path, query or {})
            data = json.loads(response.content) if response.content else None
            if response.status_code >= 400:
                detail = data.get("detail") if isinstance(data, dict) else None
                data = {"detail": str(detail or "")}
            reads[key] = {"status": response.status_code, "data": data}
        return reads

    # What is compared is what the reads return, over every template and
    # role: a loaded machine must not turn that into a test of the budget.
    @mock.patch.object(dry_run, "TIMEOUT_SECONDS", 120)
    def test_previews_equal_real_reads(self):
        for number, name in enumerate(call_templates.names()):
            call = self.import_template(name)
            actions, _ = self.walk(call)
            description = self.client.get(
                factories.CallFactory.get_protected_url(call, "workflow-description")
            ).data
            roles = [a["key"] for a in description["actors"] if a["key"] != "system"]
            cuts = sorted({len(actions) // 2, len(actions)})
            # Each role is read at one of the cuts, and which one turns with
            # the template: every template is read halfway and at the end,
            # and every role at both over the templates, for half the replays
            # of reading every role at every cut.
            for index, role in enumerate(roles):
                upto = cuts[(index + number) % len(cuts)]
                with self.subTest(template=name, actions=upto, role=role):
                    preview = self.ok(call, actions[:upto], view_as={"role": role})[
                        "view_as"
                    ]["responses"]
                    sid = transaction.savepoint()
                    try:
                        real = self.real_reads(call, actions[:upto], role)
                    finally:
                        transaction.savepoint_rollback(sid)
                    preview, real = by_step(preview), by_step(real)
                    self.assertEqual(set(preview), set(real))
                    # The real holder signed in holds a token; a preview
                    # carries none.
                    self.assertFalse(
                        [n for n in preview["me"]["data"] if n.startswith("token")]
                    )
                    for field in list(real["me"]["data"]):
                        if field.startswith("token"):
                            real["me"]["data"].pop(field)
                    for key in real:
                        self.assertEqual(
                            normalised(preview[key]), normalised(real[key]), key
                        )


class LaterFeaturesTest(DryRunMixin, test.APITestCase):
    """Round steps, the steps after the award, budgets and domain panels."""

    def at_round_step(self, data):
        return any(step["round_step"] for step in data["next"]["steps"])

    def test_a_round_step_is_one_action_for_the_round(self):
        call = self.import_template("regular_access")
        actions, data = self.walk(call, stop=self.at_round_step)
        step = next(s for s in data["next"]["steps"] if s["round_step"])
        refused = self.dry(
            call, actions + [{"step": step["step"], "outcome": step["outcomes"][0]}]
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("once per round", str(refused.data))
        before = row_counts()
        data = self.ok(
            call,
            actions
            + [
                {
                    "step": step["step"],
                    "round_step": True,
                    "outcome": step["outcomes"][0],
                }
            ],
        )
        self.assertEqual(row_counts(), before)
        state = self.step(data, step["step"])
        self.assertTrue(state["round_scoped"])
        self.assertEqual(state["status"], "completed")
        self.assertTrue(state["extra"]["round_step"]["opened"])
        self.assertIn("round_step_opened", self.kinds(data))
        self.assertIn("round_report_submitted", self.kinds(data))
        self.assertEqual(state["extra"]["round_step"]["reports_submitted"], 1)
        notifications = {m["notification"] for m in data["messages"]}
        self.assertIn("proposal.round_report_requested", notifications)

    def test_extension_and_final_report_after_the_award(self):
        call = self.import_template("extreme_scale")
        actions, data = self.walk(call)
        self.assertEqual(data["proposal_state"], ProposalStates.ACCEPTED)
        self.assertTrue(data["next"]["can_extend"])
        self.assertTrue(data["next"]["can_open_final_report"])
        actions.append({"extend": True})
        for _ in range(10):
            data = self.ok(call, actions)
            steps = [s for s in data["next"]["steps"] if s["outcomes"]]
            if not steps:
                break
            step = steps[0]
            outcome = next(o for o in step["outcomes"] if o not in NEGATIVE)
            actions.append({"step": step["step"], "outcome": outcome})
        request = self.step(data, "extension_request")
        self.assertEqual((request["phase"], request["run"]), ("extension", 1))
        self.assertEqual(self.step(data, "extension_decision")["outcome"], "approved")
        self.assertEqual(data["proposal_state"], ProposalStates.ACCEPTED)
        self.assertIn("project", self.kinds(data))
        self.assertTrue(data["next"]["can_extend"])

        actions.append({"final_report": True})
        for _ in range(10):
            data = self.ok(call, actions)
            steps = [s for s in data["next"]["steps"] if s["outcomes"]]
            if not steps:
                break
            step = steps[0]
            outcome = next(o for o in step["outcomes"] if o not in NEGATIVE)
            actions.append({"step": step["step"], "outcome": outcome})
        self.assertEqual(
            self.step(data, "final_report_approval")["outcome"], "approved"
        )
        self.assertFalse(data["next"]["can_open_final_report"])

        # Nothing of the runs, the project or their mail survives.
        before = row_counts()
        mail.outbox = []
        with mock.patch("celery.app.task.Task.apply_async") as apply_async:
            with self.captureOnCommitCallbacks() as callbacks:
                self.ok(call, actions)
        self.assertEqual(row_counts(), before)
        self.assertEqual(mail.outbox, [])
        apply_async.assert_not_called()
        self.assertEqual(callbacks, [])

    def budgeted_call(self, enforcement):
        call = self.import_template("ai_for_science")
        call.budget_enforcement = enforcement
        call.save(update_fields=["budget_enforcement"])
        requested = models.RequestedOffering.objects.filter(call=call).first()
        marketplace_factories.OfferingComponentFactory(
            offering=requested.offering,
            type="cpu",
            billing_type="limit",
            min_value=1,
            max_value=1000,
        )
        rule = models.CallOfferingComponent.objects.create(
            requested_offering=requested, component_type="cpu"
        )
        budget = models.CallResourcePool.objects.create(
            offering_component=rule, offered_total=1000
        )
        return call, budget

    def at_decision(self, data):
        return any(s["step"] == "allocation_decision" for s in data["next"]["steps"])

    def test_budget_left_and_spent(self):
        call, budget = self.budgeted_call("hard")
        actions, data = self.walk(call, stop=self.at_decision)
        (entry,) = self.step(data, "allocation_decision")["extra"]["budget"]
        self.assertEqual(
            (entry["offering"], entry["remaining"], entry["fits"]), (0, 1000, True)
        )
        self.assertTrue(data["next"]["can_exhaust_budget"])
        approve = {"step": "allocation_decision", "outcome": "approved"}
        spent = self.ok(call, actions + [{"exhaust_budget": True}])
        (entry,) = self.step(spent, "allocation_decision")["extra"]["budget"]
        self.assertFalse(entry["fits"])
        refused = self.dry(call, actions + [{"exhaust_budget": True}, approve])
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("budget", str(refused.data))
        budget.refresh_from_db()
        self.assertEqual(budget.offered_total, 1000)

    def test_the_latest_rounds_pool_covers_the_synthetic_round(self):
        call, budget = self.budgeted_call("hard")
        latest = factories.RoundFactory(call=call)
        models.CallResourcePool.objects.create(
            offering_component=budget.offering_component,
            round=latest,
            offered_total=300,
        )
        actions, data = self.walk(call, stop=self.at_decision)
        entries = self.step(data, "allocation_decision")["extra"]["budget"]
        self.assertEqual(
            sorted((entry["scope"], entry["remaining"]) for entry in entries),
            [("call", 1000), ("round", 300)],
        )

    def test_a_soft_budget_is_overridden_with_a_reason(self):
        call, budget = self.budgeted_call("soft")
        actions, _ = self.walk(call, stop=self.at_decision)
        data = self.ok(
            call,
            actions
            + [
                {"exhaust_budget": True},
                {
                    "step": "allocation_decision",
                    "outcome": "approved",
                    "override_budget": "Strategic project.",
                },
            ],
        )
        self.assertEqual(self.step(data, "allocation_decision")["outcome"], "approved")
        budget.refresh_from_db()
        self.assertEqual(budget.offered_total, 1000)

    def test_view_as_a_domain_panel_role(self):
        call = self.import_template("regular_access")
        panels = models.CallPanel.objects.count()
        data = self.ok(
            call,
            [{"step": "administrative_check", "outcome": "eligible"}],
            view_as={"role": "proposal_role:domain_panel_chair"},
        )
        self.assertEqual(data["view_as"]["responses"]["proposal"]["status"], 200)
        self.assertEqual(models.CallPanel.objects.count(), panels)
