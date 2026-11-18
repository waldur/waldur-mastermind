"""Checks another app registers for what a proposal must pass before
submission (``submission_checks``): the submit validators, the re-check under
the row lock, ``can_submit`` and the submission of requested changes all
consult them."""

from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.proposal import change_requests, models, submission_checks
from waldur_mastermind.proposal.enums import (
    CallStates,
    ChangeRequestParts,
    ProposalStates,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.models import CallWorkflowStep
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_change_requests import ChangeRequestBase
from waldur_mastermind.proposal.tests.test_extensions_and_final_report import (
    PostAwardBase,
)
from waldur_mastermind.proposal.tests.test_workflow_dry_run import DryRunMixin

KEY = "forms"
REASON = "Fill in every form before submitting."


def isolate_registry(testcase):
    """Run ``testcase`` with no checks but those it registers, and put back
    what was registered before (by another app) once it ends."""
    saved = dict(submission_checks._checks)
    submission_checks._checks.clear()

    def restore():
        submission_checks._checks.clear()
        submission_checks._checks.update(saved)

    testcase.addCleanup(restore)


def broken(proposal):
    raise RuntimeError("internal detail")


def blocked_by_summary(proposal):
    """A check that reads the proposal: refused while its summary says so."""
    if proposal.project_summary == "blocked":
        return REASON
    return None


class RegistryTest(test.APITestCase):
    def setUp(self):
        isolate_registry(self)

    def test_checks_run_in_registration_order(self):
        submission_checks.register_submission_check("first", lambda p: "First.")
        submission_checks.register_submission_check("second", lambda p: "Second.")

        self.assertEqual(
            [c.key for c in submission_checks.registered_checks()],
            ["first", "second"],
        )
        self.assertEqual(submission_checks.first_failure(object()), ("first", "First."))

    def test_registering_a_key_twice_keeps_one_check(self):
        submission_checks.register_submission_check(KEY, lambda p: "Old.")
        submission_checks.register_submission_check("other", lambda p: None)
        submission_checks.register_submission_check(KEY, lambda p: "New.")

        checks = submission_checks.registered_checks()
        self.assertEqual([c.key for c in checks], [KEY, "other"])
        self.assertEqual(submission_checks.first_failure(object()), (KEY, "New."))

    def test_unregistering_removes_the_check(self):
        submission_checks.register_submission_check(KEY, lambda p: "Refused.")
        submission_checks.unregister_submission_check(KEY)
        submission_checks.unregister_submission_check("never-registered")

        self.assertEqual(submission_checks.registered_checks(), [])
        self.assertIsNone(submission_checks.first_failure(object()))

    def test_parts_must_be_a_non_empty_collection(self):
        with self.assertRaises(TypeError):
            submission_checks.register_submission_check(
                KEY, lambda p: None, parts=ChangeRequestParts.TEAM
            )
        with self.assertRaises(ValueError):
            submission_checks.register_submission_check(KEY, lambda p: None, parts=[])
        with self.assertRaises(TypeError):
            submission_checks.register_submission_check(KEY, "not callable")
        self.assertEqual(submission_checks.registered_checks(), [])

    def test_parts_must_be_ones_a_request_for_changes_unlocks(self):
        for parts in (
            ["teams"],
            ["field:descripton"],
            [ChangeRequestParts.TEAM, "documnets"],
            # A post-award run's changes consult no registered check.
            [ChangeRequestParts.RUN_FORM],
            [ChangeRequestParts.RUN_REQUEST],
        ):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                submission_checks.register_submission_check(
                    KEY, lambda p: None, parts=parts
                )
        self.assertEqual(submission_checks.registered_checks(), [])

        submission_checks.register_submission_check(
            KEY,
            lambda p: None,
            parts=[
                *ChangeRequestParts.OTHER_PARTS,
                ChangeRequestParts.field("description"),
                ChangeRequestParts.field(ChangeRequestParts.DURATION_FIELD),
            ],
        )
        self.assertEqual(len(submission_checks.registered_checks()), 1)

    def test_a_raising_check_fails_with_a_generic_reason(self):
        later = mock.Mock(return_value=None)
        submission_checks.register_submission_check(KEY, broken)
        submission_checks.register_submission_check("later", later)
        proposal = mock.Mock(uuid="abc123")

        with self.assertLogs(submission_checks.logger, "ERROR") as logs:
            failure = submission_checks.first_failure(proposal)

        self.assertEqual(failure, (KEY, submission_checks.CHECK_ERROR_MESSAGE))
        self.assertNotIn("internal detail", str(failure[1]))
        self.assertIn(repr(KEY), logs.output[0])
        self.assertIn("abc123", logs.output[0])
        later.assert_not_called()

    def test_changes_run_only_the_checks_on_an_unlocked_part(self):
        submission_checks.register_submission_check("first", lambda p: "First.")
        submission_checks.register_submission_check(
            "team", lambda p: "Team.", parts=[ChangeRequestParts.TEAM]
        )

        self.assertEqual(
            submission_checks.first_failure(object(), parts={"documents"}), None
        )
        self.assertEqual(
            submission_checks.first_failure(
                object(), parts={"documents", ChangeRequestParts.TEAM}
            ),
            ("team", "Team."),
        )


class SubmitTest(test.APITestCase):
    """A draft that the built-in rules let through, and a check registered as
    an app's ``AppConfig.ready()`` would."""

    def setUp(self):
        isolate_registry(self)
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        factories.CallWorkflowStepFactory(
            call=self.fixture.call, step="administrative_check", duration_in_days=5
        )
        self.client.force_authenticate(self.proposal.created_by)

    def _submit(self):
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="submit")
        )

    def _can_submit(self):
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["can_submit"]

    def _block(self):
        models.Proposal.objects.filter(pk=self.proposal.pk).update(
            project_summary="blocked"
        )

    def test_without_checks_submission_goes_through(self):
        self.assertEqual(self._can_submit(), {"can_submit": True, "error": None})

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_failing_check_refuses_submission_with_its_reason(self):
        submission_checks.register_submission_check(KEY, blocked_by_summary)
        self._block()

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {KEY: [REASON]})
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

    def test_a_passing_check_lets_submission_through(self):
        check = mock.Mock(side_effect=blocked_by_summary)
        submission_checks.register_submission_check(KEY, check)

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertNotEqual(self.proposal.state, ProposalStates.DRAFT)
        # Once by the validators, once under the lock.
        self.assertEqual(check.call_count, 2)

    def test_can_submit_reports_the_check(self):
        submission_checks.register_submission_check(KEY, blocked_by_summary)
        self.assertEqual(self._can_submit(), {"can_submit": True, "error": None})

        self._block()

        self.assertEqual(self._can_submit(), {"can_submit": False, "error": REASON})
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.can_submit(), (False, REASON))

    def test_a_raising_check_refuses_submission_but_not_reading(self):
        submission_checks.register_submission_check(KEY, broken)
        generic = str(submission_checks.CHECK_ERROR_MESSAGE)

        with self.assertLogs(submission_checks.logger, "ERROR"):
            listed = self.client.get(factories.ProposalFactory.get_list_url())
            verdict = self._can_submit()
            response = self._submit()

        self.assertEqual(listed.status_code, status.HTTP_200_OK, listed.data)
        [row] = [row for row in listed.data if row["uuid"] == self.proposal.uuid.hex]
        self.assertEqual(row["can_submit"], {"can_submit": False, "error": generic})
        self.assertEqual(verdict, {"can_submit": False, "error": generic})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {KEY: [generic]})
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

    def test_built_in_rules_are_reported_before_registered_checks(self):
        submission_checks.register_submission_check(KEY, lambda p: REASON)
        self.proposal.round.call.state = CallStates.DRAFT
        self.proposal.round.call.save()

        verdict = self._can_submit()

        self.assertFalse(verdict["can_submit"])
        self.assertNotEqual(verdict["error"], REASON)

    def test_a_change_after_the_validators_is_caught_under_the_lock(self):
        """The validators pass; a concurrent request then makes the proposal
        fail the check before submit takes the row lock."""
        calls = []

        def check(proposal):
            calls.append(proposal.project_summary)
            reason = blocked_by_summary(proposal)
            if len(calls) == 1:
                # Landed just after the validators read the proposal.
                self._block()
            return reason

        submission_checks.register_submission_check(KEY, check)

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {KEY: [REASON]})
        self.assertEqual(calls, [self.proposal.project_summary, "blocked"])
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)
        self.assertIsNone(self.proposal.submitted_at)
        self.assertFalse(self.proposal.workflow_step_instances.exists())


class SubmitChangesTest(ChangeRequestBase):
    """While changes are asked of the applicant, a check applies only when it
    names one of the parts the requests unlocked."""

    def setUp(self):
        super().setUp()
        isolate_registry(self)
        self.proposal.project_summary = "blocked"
        self.proposal.save(update_fields=["project_summary"])

    def can_submit(self):
        self.client.force_authenticate(self.applicant)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["can_submit"]

    def test_a_check_on_an_unlocked_part_holds_the_changes(self):
        submission_checks.register_submission_check(
            KEY, blocked_by_summary, parts=[ChangeRequestParts.TEAM]
        )
        self.request_changes(parts=[ChangeRequestParts.TEAM])

        self.assertEqual(self.can_submit(), {"can_submit": False, "error": REASON})
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {KEY: [REASON]})
        self.assertTrue(change_requests.open_applicant_requests(self.proposal).exists())

    def test_a_check_passing_on_an_unlocked_part_lets_the_changes_through(self):
        submission_checks.register_submission_check(
            KEY, blocked_by_summary, parts=[ChangeRequestParts.TEAM]
        )
        self.request_changes(parts=[ChangeRequestParts.TEAM])
        models.Proposal.objects.filter(pk=self.proposal.pk).update(
            project_summary="Fixed"
        )

        self.assertEqual(self.can_submit(), {"can_submit": True, "error": None})
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_check_on_a_locked_part_does_not_hold_the_changes(self):
        submission_checks.register_submission_check(
            KEY, blocked_by_summary, parts=[ChangeRequestParts.TEAM]
        )
        self.request_changes(parts=["field:project_summary"])

        self.assertEqual(self.can_submit(), {"can_submit": True, "error": None})
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_check_without_parts_does_not_apply_to_changes(self):
        submission_checks.register_submission_check(KEY, blocked_by_summary)
        self.request_changes(parts=["field:project_summary", "documents"])

        self.assertEqual(self.can_submit(), {"can_submit": True, "error": None})
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class RunChangesTest(PostAwardBase):
    """Changes to a post-award run leave the accepted proposal as it was:
    submit_changes runs no registered check, and can_submit says the same."""

    def setUp(self):
        super().setUp()
        isolate_registry(self)
        CallWorkflowStep.objects.filter(pk=self.approval_step.pk).update(
            allows_change_requests=True
        )

    def test_registered_checks_hold_neither_can_submit_nor_the_changes(self):
        self._post(self.call_manager, "open_final_report")
        run = self.proposal.runs.get()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        response = self._post(
            self.call_manager,
            "request_changes",
            {
                "step_uuid": self._active(run).uuid.hex,
                "message": "Please revise",
                "parts": [ChangeRequestParts.RUN_FORM],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        check = mock.Mock(return_value=REASON)
        submission_checks.register_submission_check(KEY, check)
        submission_checks.register_submission_check(
            "on-parts", check, parts=list(ChangeRequestParts.OTHER_PARTS)
        )

        self.client.force_authenticate(self.applicant)
        response = self.client.get(self._url(None))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["can_submit"], {"can_submit": True, "error": None}
        )

        response = self._post(self.applicant, "submit_changes")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        check.assert_not_called()


class DryRunTest(DryRunMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        isolate_registry(self)

    def test_the_dry_run_does_not_run_registered_checks(self):
        check = mock.Mock(return_value=REASON)
        submission_checks.register_submission_check(
            KEY, check, parts=list(ChangeRequestParts.OTHER_PARTS)
        )
        call = self.import_template("ai_factory_large_scale")

        self.ok(
            call,
            [
                {"step": "administrative_check", "request_changes": True},
                {"submit_changes": True},
            ],
        )

        check.assert_not_called()
