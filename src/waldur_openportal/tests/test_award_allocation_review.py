"""
Updates to a live award are applied at once; only an allocation increase
over the credits last approved waits for a local reviewer.
"""

import datetime
import decimal
import json
from unittest import mock

import openportal
from django.test import TestCase
from rest_framework import test

from waldur_core.core.enums import ReviewStates
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import exceptions, models, serializers, tasks
from waldur_openportal.board import OpenPortalBoard
from waldur_openportal.extension import OpenPortalExtension

from .test_award_membership import DESTINATION, IDENTIFIER, MembershipSyncFixture

D = decimal.Decimal
TODAY = datetime.date.today()
# Allocations above this many credits need approval by the project template.
APPROVAL_LIMIT = 100


def award(allocation=None, **fields):
    payload = dict(fields)
    if allocation is not None:
        payload["allocation"] = allocation
    for key, value in list(payload.items()):
        if isinstance(value, datetime.date):
            payload[key] = value.isoformat()
    return openportal.AwardDetails(json.dumps(payload))


class AllocationReviewTest(MembershipSyncFixture, TestCase):
    """
    Only an allocation increase of at least one credit over the credits last
    approved waits for review; every other change to a live award applies.
    """

    def setUp(self):
        super().setUp()
        self.project_template.convert_to_credits.side_effect = (
            lambda allocation: allocation.size
        )
        self.project_template.action_is_rejected.return_value = False
        self.project_template.action_needs_approval.side_effect = (
            lambda allocation=None: (allocation or 0) > APPROVAL_LIMIT
        )
        self.managed_project.approved_credits = D("1000.0")
        self.managed_project.pending_allocation = None
        self.credits = D("1000.0")
        self.utils.get_project_credits.side_effect = lambda project: self.credits
        self.utils.set_project_credits.side_effect = self.set_credits

        # Audit entries need a real ManagedProject; which ones are written is
        # asserted where it matters.
        record_patcher = mock.patch(
            "waldur_openportal.board.models.ManagedProjectAuditEntry.record"
        )
        self.record = record_patcher.start()
        self.addCleanup(record_patcher.stop)

    def set_credits(self, project, credits):
        # Credits are stored to a fixed precision, not as the binary
        # expansion of the float the allocation arrived as.
        self.credits = D(credits).quantize(D("0.0000001"))

    def update(self, details, force_approve=False, max_approved_credits=None):
        return self.board.update_award(
            openportal.ProjectIdentifier(IDENTIFIER),
            details,
            force_approve=force_approve,
            max_approved_credits=max_approved_credits,
        )

    def hold_pending(self):
        """What the model does when the award is held: it becomes pending."""
        self.managed_project.is_approved.return_value = False
        self.managed_project.is_pending.return_value = True

    def assert_not_held(self):
        self.managed_project.set_needs_approval.assert_not_called()

    def test_increase_waits_but_everything_else_is_applied(self):
        new_end = TODAY + datetime.timedelta(days=90)
        details = award(
            "1500 credits",
            name="Renamed",
            end_date=new_end,
            start_date=TODAY,
            membership_control="locked",
            members={"alice@example.com": "admin"},
        )

        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(details)

        self.utils.set_project_credits.assert_not_called()
        self.assertEqual(self.managed_project.pending_allocation, D("1500.0"))
        self.managed_project.set_needs_approval.assert_called_once()
        comment = self.managed_project.set_needs_approval.call_args.kwargs["comment"]
        self.assertIn("1500", comment)
        self.assertIn("approved: 1000", comment)
        # The reviewer can see in the audit trail what is waiting.
        self.assertIn("1500", self.record.call_args.kwargs["note"])
        self.assertEqual(self.project.name, "Renamed")
        self.assertEqual(self.project.end_date, new_end)
        self.assertEqual(self.project.start_date, TODAY)
        self.utils.remove_project_member.assert_called_once_with(
            self.project, "bob@example.com"
        )

    def test_extension_applies_while_increase_is_pending(self):
        self.hold_pending()
        self.managed_project.pending_allocation = D("1500.0")
        new_end = TODAY + datetime.timedelta(days=30)

        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(award("1500 credits", end_date=new_end))

        self.assertEqual(self.project.end_date, new_end)
        self.utils.set_project_credits.assert_not_called()
        self.assertEqual(self.managed_project.pending_allocation, D("1500.0"))

    def test_extension_without_allocation_keeps_the_increase_waiting(self):
        self.hold_pending()
        self.managed_project.pending_allocation = D("1500.0")
        new_end = TODAY + datetime.timedelta(days=30)

        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(award(end_date=new_end))

        self.assertEqual(self.project.end_date, new_end)
        self.assertEqual(self.managed_project.pending_allocation, D("1500.0"))
        self.assert_not_held()

    def test_rounding_difference_is_not_a_change(self):
        # Below the seven places credits are stored and compared with.
        self.credits = D("999.99999999")

        self.update(award("1000.0 credits"))

        self.utils.set_project_credits.assert_not_called()
        self.assert_not_held()

    def test_difference_under_one_credit_is_applied_not_dropped(self):
        """
        A difference finer than the precision the allocation was sent with
        is still a change in credits: it is applied, without review.
        """
        self.credits = D("999.9634")

        self.update(award("1000.0 credits"))

        self.utils.set_project_credits.assert_called_once_with(
            self.project, D("1000.0")
        )
        self.assert_not_held()

    def test_increase_under_one_credit_is_applied_without_review(self):
        self.update(award("1000.5 credits"))

        self.utils.set_project_credits.assert_called_once_with(
            self.project, D("1000.5")
        )
        self.assert_not_held()
        # Not approved by anyone, so the baseline stays where it was.
        self.assertEqual(self.managed_project.approved_credits, D("1000.0"))

    def test_small_increases_cannot_add_up_unreviewed(self):
        self.update(award("1000.6 credits"))
        self.assertEqual(self.credits, D("1000.6"))
        self.assert_not_held()

        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(award("1001.2 credits"))

        # Only 0.6 over the current credits, but 1.2 over the approved ones.
        self.assertEqual(self.credits, D("1000.6"))
        self.assertEqual(self.managed_project.pending_allocation, D("1001.2"))
        self.managed_project.set_needs_approval.assert_called_once()

    def test_decrease_is_applied(self):
        self.update(award("800 credits"))

        self.utils.set_project_credits.assert_called_once_with(self.project, D("800"))
        self.assert_not_held()

    def test_increase_within_approval_limit_moves_approved_credits(self):
        self.managed_project.approved_credits = D("10")
        self.credits = D("10")

        self.update(award("50 credits"))

        self.utils.set_project_credits.assert_called_once_with(self.project, D("50"))
        self.assertEqual(self.managed_project.approved_credits, D("50"))
        self.assert_not_held()

    def test_withdrawn_increase_clears_it_and_leaves_the_award_approved(self):
        self.hold_pending()
        self.managed_project.pending_allocation = D("1500.0")

        self.update(award("1000 credits"))

        self.assertIsNone(self.managed_project.pending_allocation)
        self.managed_project.set_needs_approval.assert_called_once_with(False)
        self.utils.set_project_credits.assert_not_called()

    def test_approved_increase_is_applied_and_becomes_the_baseline(self):
        # A reviewer approved the award; managed_project_approved re-sends the
        # stored details with force_approve.
        self.managed_project.pending_allocation = D("1500.0")

        self.update(award("1500 credits"), force_approve=True)

        self.utils.set_project_credits.assert_called_once_with(
            self.project, D("1500.0")
        )
        self.assertEqual(self.managed_project.approved_credits, D("1500.0"))
        self.assertIsNone(self.managed_project.pending_allocation)
        self.assert_not_held()

    def test_approved_credits_start_from_current_credits(self):
        self.managed_project.approved_credits = None
        self.credits = D("1000.04")

        self.update(award("1000.04 credits"))

        self.assertEqual(self.managed_project.approved_credits, D("1000.04"))

    def test_force_approve_applies_no_more_than_the_approved_figure(self):
        """
        The details are re-sent when the approval task runs; the awarding
        portal may have raised the figure after the reviewer approved it.
        """
        self.managed_project.pending_allocation = D("1500.0")

        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(
                award("2500 credits"),
                force_approve=True,
                max_approved_credits=D("1500"),
            )

        self.utils.set_project_credits.assert_not_called()
        self.assertEqual(self.managed_project.approved_credits, D("1000.0"))
        self.assertEqual(self.managed_project.pending_allocation, D("2500"))

    def test_increase_over_the_maximum_refuses_only_the_increase(self):
        self.project_template.action_is_rejected.side_effect = (
            lambda allocation=None: (allocation or 0) > 2000
        )
        new_end = TODAY + datetime.timedelta(days=60)

        with self.assertRaisesMessage(
            exceptions.ManagedProjectRejectedError, "maximum credit limit"
        ):
            self.update(award("3000 credits", name="Renamed", end_date=new_end))

        # The running award is not rejected: only the increase is refused.
        self.managed_project.reject.assert_not_called()
        self.utils.set_project_credits.assert_not_called()
        self.assertEqual(self.project.name, "Renamed")
        self.assertEqual(self.project.end_date, new_end)
        self.assertIn("maximum credit limit", self.record.call_args.kwargs["note"])

    def test_increase_over_the_maximum_supersedes_a_waiting_one(self):
        self.project_template.action_is_rejected.side_effect = (
            lambda allocation=None: (allocation or 0) > 2000
        )
        self.hold_pending()
        self.managed_project.pending_allocation = D("1500.0")

        with self.assertRaises(exceptions.ManagedProjectRejectedError):
            self.update(award("3000 credits"))

        self.managed_project.reject.assert_not_called()
        self.assertIsNone(self.managed_project.pending_allocation)
        self.managed_project.set_needs_approval.assert_called_once_with(False)

    def test_decrease_still_over_the_maximum_is_applied_to_a_live_award(self):
        self.project_template.action_is_rejected.return_value = True

        self.update(award("800 credits"))

        self.managed_project.reject.assert_not_called()
        self.utils.set_project_credits.assert_called_once_with(self.project, D("800"))

    def test_increase_in_grace_period_is_recorded_not_applied(self):
        self.project.is_in_grace_period = True

        self.update(award("1000.5 credits"))

        self.utils.set_project_credits.assert_not_called()
        self.assertIn("grace period", self.record.call_args.kwargs["note"])

    def test_award_waiting_to_be_attached_is_not_live(self):
        """
        Pending with a project but no pending increase, e.g. waiting to be
        attached to an existing project: nothing is applied, as before.
        """
        self.hold_pending()

        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(award("1000 credits", name="Renamed"))

        self.project.save.assert_not_called()
        self.assertNotEqual(self.project.name, "Renamed")

    def test_unapproved_award_of_a_template_needing_approval_is_not_live(self):
        self.hold_pending()
        self.project_template.action_needs_approval.side_effect = None
        self.project_template.action_needs_approval.return_value = True

        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(award(name="Renamed"))

        self.project.save.assert_not_called()


class AwardEndDateOnArrivalTest(TestCase):
    def setUp(self):
        patcher = mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.board = OpenPortalBoard(DESTINATION)
        self.identifier = openportal.ProjectIdentifier(IDENTIFIER)

    def test_new_award_ending_today_is_refused(self):
        with self.assertRaisesMessage(
            exceptions.ManagedProjectRejectedError, "is not in the future"
        ):
            self.board.create_award(
                self.identifier, award("10 credits", end_date=TODAY, name="x")
            )

        self.assertFalse(models.ManagedProject.objects.exists())

    def test_ended_award_is_not_recreated(self):
        with mock.patch.object(OpenPortalBoard, "create_award") as create_award:
            with self.assertRaises(exceptions.ManagedProjectRejectedError):
                self.board.update_award(
                    self.identifier,
                    award("10 credits", end_date=TODAY - datetime.timedelta(days=1)),
                )

        create_award.assert_not_called()

    def test_missing_award_that_still_runs_is_recreated(self):
        details = award("10 credits", end_date=TODAY + datetime.timedelta(days=1))

        with mock.patch.object(OpenPortalBoard, "create_award") as create_award:
            self.board.update_award(self.identifier, details)

        create_award.assert_called_once_with(
            identifier=self.identifier, details=details, force_request_approval=True
        )

    def test_existing_award_keeps_the_grace_allowance(self):
        models.ManagedProject.objects.create(
            destination=DESTINATION, identifier=IDENTIFIER, details={}
        )

        # Not refused for its end date: it gets as far as resolving its
        # project template.
        with mock.patch.object(
            OpenPortalBoard, "_get_project_template", return_value=None
        ):
            with self.assertRaisesMessage(
                exceptions.ManagedProjectRejectedError, "project class"
            ):
                self.board.create_award(
                    self.identifier,
                    award(end_date=TODAY - datetime.timedelta(days=3)),
                )


class RejectEndedWaitingAwardsTest(TestCase):
    def setUp(self):
        self.robot = structure_factories.UserFactory()
        patcher = mock.patch(
            "waldur_openportal.tasks.utils.get_openportal_robot",
            return_value=self.robot,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, identifier, end_date, state=ReviewStates.PENDING, project=None):
        return models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier=identifier,
            details={"name": identifier, "end_date": end_date.isoformat()},
            state=state,
            project=project,
        )

    def test_rejects_only_ended_waiting_awards_without_a_project(self):
        past = TODAY - datetime.timedelta(days=3)
        ended = self.make("ended.someportal", TODAY)
        draft = self.make("draft.someportal", past, ReviewStates.DRAFT)
        running = self.make("running.someportal", TODAY + datetime.timedelta(days=1))
        attached = self.make(
            "attached.someportal", past, project=structure_factories.ProjectFactory()
        )
        approved = self.make("approved.someportal", past, ReviewStates.APPROVED)

        with mock.patch.object(models.ManagedProject, "_notify") as notify:
            tasks.reject_ended_waiting_awards()

        for managed_project, state in (
            (ended, ReviewStates.REJECTED),
            (draft, ReviewStates.REJECTED),
            (running, ReviewStates.PENDING),
            (attached, ReviewStates.PENDING),
            (approved, ReviewStates.APPROVED),
        ):
            managed_project.refresh_from_db()
            self.assertEqual(managed_project.state, state, managed_project.identifier)

        # The awarding portal is told, as it is when a site admin rejects.
        self.assertEqual(
            notify.call_args_list,
            [mock.call("award_rejected"), mock.call("award_rejected")],
        )
        self.assertEqual(ended.reviewed_by, self.robot)
        self.assertIn(TODAY.isoformat(), ended.review_comment)
        self.assertEqual(
            set(
                models.ManagedProjectAuditEntry.objects.filter(
                    event_type=models.ManagedProjectAuditEventType.REJECTED,
                    performed_by=self.robot,
                ).values_list("identifier", flat=True)
            ),
            {"ended.someportal", "draft.someportal"},
        )

    def test_one_failing_award_does_not_stop_the_others(self):
        past = TODAY - datetime.timedelta(days=3)
        failing = self.make("failing.someportal", past)
        other = self.make("other.someportal", past)
        original = models.ManagedProject.reject

        def reject(managed_project, *args, **kwargs):
            if managed_project.identifier == failing.identifier:
                raise RuntimeError("boom")
            return original(managed_project, *args, **kwargs)

        with (
            mock.patch.object(models.ManagedProject, "_notify"),
            mock.patch.object(models.ManagedProject, "reject", reject),
        ):
            tasks.reject_ended_waiting_awards()

        failing.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(failing.state, ReviewStates.PENDING)
        self.assertEqual(other.state, ReviewStates.REJECTED)

    def test_award_without_an_end_date_is_left_waiting(self):
        managed_project = models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier="open.someportal",
            details={"name": "open"},
            state=ReviewStates.PENDING,
        )

        tasks.reject_ended_waiting_awards()

        managed_project.refresh_from_db()
        self.assertEqual(managed_project.state, ReviewStates.PENDING)

    def test_task_runs_daily(self):
        schedule = OpenPortalExtension.celery_tasks()[
            "waldur-openportal-reject-ended-waiting-awards"
        ]
        self.assertEqual(
            schedule["task"], "waldur_openportal.reject_ended_waiting_awards"
        )


class AllocationReviewFieldsApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.managed_project = models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier=IDENTIFIER,
            details={"name": "Some award"},
            project=self.fixture.project,
            state=ReviewStates.PENDING,
            approved_credits=D("1000"),
            pending_allocation=D("1500.5"),
        )

    def listed(self, user):
        self.client.force_authenticate(user)
        response = self.client.get("/api/openportal-managed-projects/")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["identifier"]: row for row in response.data}

    def test_staff_and_project_customer_owner_see_the_figures(self):
        for user in (self.fixture.staff, self.fixture.owner):
            row = self.listed(user)[IDENTIFIER]
            self.assertEqual(D(row["approved_credits"]), D("1000"))
            self.assertEqual(D(row["pending_allocation"]), D("1500.5"))

    def test_user_who_cannot_see_the_award_does_not_see_the_figures(self):
        self.assertNotIn(IDENTIFIER, self.listed(self.fixture.user))

    def test_figures_are_read_only(self):
        fields = serializers.ManagedProjectSerializer().fields
        self.assertTrue(fields["approved_credits"].read_only)
        self.assertTrue(fields["pending_allocation"].read_only)


class AllocationReviewOnRealAwardTest(test.APITestCase):
    """
    The review of an allocation increase end to end, on real rows: the board
    holds it, a reviewer approves or declines it through the API, and the
    approval task applies it.
    """

    def setUp(self):
        for patcher in (
            mock.patch(
                "waldur_openportal.config.ensure_config_loaded", return_value=True
            ),
            mock.patch("openportal.get_portal", return_value="someportal"),
            mock.patch.object(tasks.create_default_resources, "delay"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        notify_patcher = mock.patch.object(models.ManagedProject, "_notify")
        self.notify = notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

        # Credits are held in memory: the accounting behind them is not
        # what is under test.
        self.credits = D("1000")
        for name, side_effect in (
            ("get_project_credits", lambda project, silent=False: self.credits),
            ("set_project_credits", self.set_credits),
        ):
            patcher = mock.patch(f"waldur_openportal.utils.{name}")
            patcher.start().side_effect = side_effect
            self.addCleanup(patcher.stop)

        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        end_date = TODAY + datetime.timedelta(days=365)
        self.project.end_date = end_date
        self.project.save(update_fields=["end_date"])
        self.template = models.ProjectTemplate.objects.create(
            name="template",
            portal="someportal",
            approval_limit=D(APPROVAL_LIMIT),
        )
        self.managed_project = models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier=IDENTIFIER,
            local_identifier="someproject.someportal",
            project=self.project,
            project_template=self.template,
            state=ReviewStates.APPROVED,
            details=json.loads(
                award("1000 credits", name="Some award", end_date=end_date).to_json()
            ),
            approved_credits=D("1000"),
        )
        self.board = OpenPortalBoard(DESTINATION)
        self.client.force_authenticate(self.fixture.staff)

    def set_credits(self, project, credits, silent=False):
        self.credits = D(credits).quantize(D("0.00001"))

    def update(self, details):
        return self.board.update_award(
            openportal.ProjectIdentifier(IDENTIFIER), details
        )

    def hold(self, allocation):
        with self.assertRaises(exceptions.ManagedProjectPendingError):
            self.update(award(allocation))
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.PENDING)

    def post(self, action, data=None):
        return self.client.post(
            f"/api/openportal-managed-projects/{IDENTIFIER}/{DESTINATION}/{action}/",
            data or {},
            format="json",
        )

    def approve(self, data=None):
        """Approve, running the approval task as soon as it is queued."""
        with mock.patch.object(
            tasks.managed_project_approved,
            "delay",
            side_effect=tasks.managed_project_approved,
        ):
            return self.post("approve", data)

    def notified(self):
        return [c.args[0] for c in self.notify.call_args_list]

    def test_approving_a_held_increase_applies_it(self):
        self.hold("1500 credits")
        self.assertEqual(self.managed_project.pending_allocation, D("1500"))

        response = self.approve({"expected_allocation": "1500"})

        self.assertEqual(response.status_code, 200, response.data)
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.APPROVED)
        self.assertIsNone(self.managed_project.pending_allocation)
        self.assertEqual(self.managed_project.approved_credits, D("1500"))
        self.assertEqual(self.credits, D("1500"))
        self.assertEqual(self.managed_project.reviewed_by, self.fixture.staff)
        self.assertIn("award_accepted", self.notified())

    def test_approval_with_a_stale_figure_is_refused(self):
        self.hold("1500 credits")
        # The awarding portal raises the figure while the reviewer looks.
        self.hold("2500 credits")

        with mock.patch.object(tasks.managed_project_approved, "delay") as delay:
            response = self.post("approve", {"expected_allocation": "1500"})

        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("2500", str(response.data))
        delay.assert_not_called()
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.PENDING)
        self.assertEqual(self.managed_project.pending_allocation, D("2500"))
        self.assertEqual(self.credits, D("1000"))

    def test_approval_task_applies_no_more_than_the_approved_figure(self):
        self.hold("1500 credits")

        with mock.patch.object(tasks.managed_project_approved, "delay") as delay:
            response = self.post("approve")
        self.assertEqual(response.status_code, 200, response.data)
        args = delay.call_args.args
        self.assertEqual(D(args[1]), D("1500"))

        # A racing update merged a larger figure into the stored details
        # before the task ran.
        self.managed_project.refresh_from_db()
        self.managed_project.set_details(award("2500 credits", name="Some award"))
        self.notify.reset_mock()

        tasks.managed_project_approved(*args)

        self.managed_project.refresh_from_db()
        self.assertEqual(self.credits, D("1000"))
        self.assertEqual(self.managed_project.state, ReviewStates.PENDING)
        self.assertEqual(self.managed_project.pending_allocation, D("2500"))
        self.assertNotIn("award_accepted", self.notified())

    @mock.patch.object(tasks.notify_users_about_rejected_allocation, "delay")
    def test_rejecting_a_held_increase_keeps_the_award_live(self, notify_users):
        self.template.allocation_units_mapping = {"gpuh": 100}
        self.template.save(update_fields=["allocation_units_mapping"])
        self.credits = D("15")  # 1500 GPU hours
        self.managed_project.approved_credits = D("15")
        self.managed_project.state = ReviewStates.APPROVED
        self.managed_project.save(update_fields=["approved_credits", "state"])
        self.template.approval_limit = D("20")
        self.template.save(update_fields=["approval_limit"])

        self.hold("2500 gpuh")
        self.assertEqual(self.managed_project.pending_allocation, D("25"))

        response = self.post("reject", {"comment": "Not this quarter."})

        self.assertEqual(response.status_code, 200, response.data)
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.APPROVED)
        self.assertIsNone(self.managed_project.pending_allocation)
        self.assertEqual(self.managed_project.approved_credits, D("15"))
        self.assertEqual(self.credits, D("15"))
        # The award is not rejected on the awarding portal, nor are the
        # project's users told their allocation was.
        self.assertNotIn("award_rejected", self.notified())
        notify_users.assert_not_called()
        entry = models.ManagedProjectAuditEntry.objects.filter(
            managed_project=self.managed_project,
            event_type=models.ManagedProjectAuditEventType.REJECTED,
        ).get()
        self.assertIn("increase to 25 credits declined", entry.note)
        self.assertIn("Not this quarter.", entry.note)
        # The stored allocation is what the project has, in the units the
        # awarding portal uses.
        self.assertEqual(
            str(self.managed_project.get_details().allocation), "1500 gpuh"
        )

        # Later updates are still applied.
        new_end = TODAY + datetime.timedelta(days=400)
        self.update(award(end_date=new_end, name="Renamed"))

        self.project.refresh_from_db()
        self.managed_project.refresh_from_db()
        self.assertEqual(self.project.end_date, new_end)
        self.assertEqual(self.project.name, "Renamed")
        self.assertEqual(self.managed_project.state, ReviewStates.APPROVED)

    @mock.patch.object(tasks.notify_users_about_rejected_allocation, "delay")
    def test_rejecting_an_award_that_is_not_live_still_rejects_it(self, notify_users):
        self.managed_project.project = None
        self.managed_project.state = ReviewStates.PENDING
        self.managed_project.save(update_fields=["project", "state"])

        response = self.post("reject")

        self.assertEqual(response.status_code, 200, response.data)
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.REJECTED)
        self.assertIn("award_rejected", self.notified())
        notify_users.assert_called_once()

    @mock.patch.object(tasks.notify_users_about_rejected_allocation, "delay")
    def test_rejecting_a_held_first_allocation_rejects_the_award(self, notify_users):
        """
        With a positive approval limit a new award is approved on arrival
        with a project and no credits, and its allocation is then held as an
        increase. There is nothing to keep running: rejecting it rejects the
        award, as it did before increases were reviewed on their own.
        """
        self.credits = D("0")
        self.managed_project.approved_credits = None
        self.managed_project.details = {"name": "Some award"}
        self.managed_project.save(update_fields=["approved_credits", "details"])
        self.hold("1500 credits")

        response = self.post("reject", {"comment": "Too large."})

        self.assertEqual(response.status_code, 200, response.data)
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.REJECTED)
        self.assertIn("award_rejected", self.notified())
        self.assertNotIn("award_changed", self.notified())
        notify_users.assert_called_once()
        self.assertEqual(self.credits, D("0"))

    def test_refused_increase_is_not_claimed_by_later_updates(self):
        self.template.max_credit_limit = D("2000")
        self.template.save(update_fields=["max_credit_limit"])
        with self.assertRaisesMessage(
            exceptions.ManagedProjectRejectedError, "maximum credit limit"
        ):
            self.update(award("3000 credits"))
        self.managed_project.refresh_from_db()
        self.assertEqual(
            str(self.managed_project.get_details().allocation), "1000 credits"
        )

        # An update that leaves the allocation out is applied, not refused.
        new_end = TODAY + datetime.timedelta(days=500)
        self.update(award(end_date=new_end))

        self.project.refresh_from_db()
        self.managed_project.refresh_from_db()
        self.assertEqual(self.project.end_date, new_end)
        self.assertEqual(self.managed_project.state, ReviewStates.APPROVED)
        self.assertEqual(self.credits, D("1000"))

    def test_resending_the_approved_figure_before_the_task_runs(self):
        self.hold("1500 credits")
        with mock.patch.object(tasks.managed_project_approved, "delay") as delay:
            response = self.post("approve", {"expected_allocation": "1500"})
        self.assertEqual(response.status_code, 200, response.data)

        # The awarding portal sends the same figure again before the
        # approval task runs: it is approved already, not held again.
        self.update(award("1500 credits"))
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.APPROVED)
        self.assertEqual(self.credits, D("1500"))

        tasks.managed_project_approved(*delay.call_args.args)

        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.APPROVED)
        self.assertEqual(self.managed_project.approved_credits, D("1500"))
        self.assertEqual(self.credits, D("1500"))

    def test_whole_unit_change_in_a_scaled_unit_is_applied(self):
        """
        With 100 GPU hours to the credit, 1555 -> 1559 GPU hours is 0.04
        credits: rounding credits at the precision the hours were sent with
        dropped it.
        """
        self.template.allocation_units_mapping = {"gpuh": 100}
        self.template.save(update_fields=["allocation_units_mapping"])
        self.credits = D("15.55")
        self.managed_project.approved_credits = D("15.55")
        self.managed_project.save(update_fields=["approved_credits"])

        self.update(award("1559 gpuh"))

        self.assertEqual(self.credits, D("15.59"))
        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.state, ReviewStates.APPROVED)
        self.assertIsNone(self.managed_project.pending_allocation)
