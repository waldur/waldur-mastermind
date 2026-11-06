"""A step only the lead of an evaluator role contributes to.

Two rapporteurs hold one role and both write an individual report; the
lead alone writes the consolidated one.
"""

from rest_framework import serializers, status, test

from waldur_mastermind.proposal import (
    call_transfer,
    contributions,
    models,
    notification_rules,
    proposal_roles,
    step_inputs,
    utils,
    workflow_service,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    ProposalRoleEligibility,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
)

Statuses = StepContributionStatuses


class LeadOnlyStepMixin(EvaluatorRoleTestMixin):
    def setUp(self):
        super().setUp()
        source = f"{ContributorSources.PROPOSAL_ROLE}:{self.role.pk}"
        self.individual = self.add_step(
            "individual-report",
            StepKinds.ASSESSMENT,
            "Individual report",
            2,
            outcome_set="done",
            contributor_source=source,
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
        )
        self.consolidated = self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            3,
            outcome_set="done",
            contributor_source=source,
            contributors_lead_only=True,
            min_contributions=1,
            max_contributions=1,
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
        )
        self.consolidated.input_steps.set([self.individual])
        self.lead = self.panel_1
        self.second = self.panel_2

    def reach_individual_step(self, mark_lead=True):
        self.reach_assignment_step()
        self.assertEqual(
            self.assign(self.lead, is_lead=True).status_code, status.HTTP_201_CREATED
        )
        self.assertEqual(self.assign(self.second).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "individual-report")
        if not mark_lead:
            models.ProposalRoleAssignment.objects.filter(
                proposal=self.proposal, role=self.role
            ).update(is_lead=False)

    def reach_consolidated_step(self, mark_lead=True):
        self.reach_individual_step(mark_lead)
        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")
        return self.instance(self.consolidated)

    def instance(self, call_step):
        return self.proposal.workflow_step_instances.get(call_step=call_step)

    def slots(self, call_step=None):
        return {
            contribution.contributor: contribution.status
            for contribution in self.instance(
                call_step or self.consolidated
            ).contributions.all()
        }

    def submit_contribution(self, user, call_step=None):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="submit_contribution"
            ),
            {"step_uuid": (call_step or self.consolidated).uuid.hex},
            format="json",
        )

    def set_lead(self, user):
        assignment = proposal_roles.active_assignments(self.proposal, self.role).get(
            user=user
        )
        self.client.force_authenticate(self.manager)
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="set_role_lead"),
            {"assignment_uuid": assignment.uuid.hex},
        )

    def awaiting_lead(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        step = next(
            item
            for item in response.data
            if item["call_step_uuid"] == self.consolidated.uuid.hex
        )
        self.assertTrue(step["contributors_lead_only"])
        return step["awaiting_lead"]


class LeadOnlySlotTest(LeadOnlyStepMixin, test.APITestCase):
    def test_every_holder_writes_the_individual_report(self):
        self.reach_individual_step()
        self.assertEqual(
            self.slots(self.individual),
            {self.lead: Statuses.PENDING, self.second: Statuses.PENDING},
        )

    def test_only_the_lead_gets_a_slot_and_may_contribute(self):
        instance = self.reach_consolidated_step()
        self.assertEqual(self.slots(), {self.lead: Statuses.PENDING})
        self.assertTrue(contributions.may_contribute(self.lead, instance))
        self.assertFalse(contributions.may_contribute(self.second, instance))

        response = self.submit_contribution(self.second)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.slots(), {self.lead: Statuses.PENDING})

        response = self.submit_contribution(self.lead)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.slots(), {self.lead: Statuses.SUBMITTED})
        self.assertIsNone(self.awaiting_lead())

    def test_the_second_still_reads_the_steps_inputs(self):
        instance = self.reach_consolidated_step()
        self.assertTrue(step_inputs.can_read_inputs(self.second, instance))
        self.assertTrue(step_inputs.can_read_inputs(self.lead, instance))
        self.assertFalse(step_inputs.can_read_inputs(self.panel_3, instance))

    def test_changing_the_lead_moves_the_slot(self):
        self.reach_consolidated_step()
        response = self.set_lead(self.second)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.slots(),
            {self.lead: Statuses.EXPIRED, self.second: Statuses.PENDING},
        )
        # Their slot is withdrawn: theirs to read, no longer to fill.
        self.assertEqual(
            self.submit_contribution(self.lead).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Back again: the first lead's slot reopens.
        self.assertEqual(self.set_lead(self.lead).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.slots(),
            {self.lead: Statuses.PENDING, self.second: Statuses.EXPIRED},
        )

    def test_a_submitted_report_stays_but_only_the_new_lead_counts(self):
        self.reach_consolidated_step()
        self.assertEqual(
            self.submit_contribution(self.lead).status_code, status.HTTP_200_OK
        )
        self.assertEqual(self.set_lead(self.second).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.slots(),
            {self.lead: Statuses.SUBMITTED, self.second: Statuses.PENDING},
        )
        # The earlier lead's report does not count towards the gates.
        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Nor does it fill the step's one contribution.
        response = self.submit_contribution(self.second)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)

    def test_revoking_the_lead_withdraws_their_slot_and_the_step_waits(self):
        self.reach_consolidated_step()
        assignment = proposal_roles.active_assignments(self.proposal, self.role).get(
            user=self.lead
        )
        self.assertEqual(
            self.revoke(assignment.uuid.hex).status_code, status.HTTP_200_OK
        )
        self.assertEqual(self.slots(), {self.lead: Statuses.EXPIRED})
        self.assertIn("mark one holder as the lead", self.awaiting_lead())

        # A new lead, assigned as such, gets a slot.
        response = self.assign(self.panel_3, is_lead=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self.slots(),
            {self.lead: Statuses.EXPIRED, self.panel_3: Statuses.PENDING},
        )
        self.assertIsNone(self.awaiting_lead())


class LeadOnlyCompletionTest(LeadOnlyStepMixin, test.APITestCase):
    """The lead alone completes a lead-only step its own role completes,
    and alone rejects the proposal at it; the call's managers still may."""

    def setUp(self):
        super().setUp()
        self.instance_ = self.reach_consolidated_step()

    def reject(self, user):
        self.proposal.refresh_from_db()
        self.client.force_authenticate(user)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="reject_workflow_step"
            ),
            {
                "step_uuid": self.proposal.current_step.uuid.hex,
                "reason": "Out of scope.",
            },
        )
        self.proposal.refresh_from_db()
        return response

    def submit_lead_report(self):
        response = self.submit_contribution(self.lead)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_second_cannot_complete(self):
        self.submit_lead_report()
        response = self.complete("done", user=self.second)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")

    def test_the_second_cannot_reject(self):
        response = self.reject(self.second)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")
        self.assertEqual(
            self.instance(self.consolidated).status, WorkflowStepInstanceStatuses.ACTIVE
        )

    def test_the_lead_completes(self):
        self.submit_lead_report()
        response = self.complete("done", user=self.lead)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.instance(self.consolidated).status,
            WorkflowStepInstanceStatuses.COMPLETED,
        )

    def test_the_lead_rejects(self):
        response = self.reject(self.lead)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_call_manager_completes(self):
        self.submit_lead_report()
        response = self.complete("done", user=self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.instance(self.consolidated).status,
            WorkflowStepInstanceStatuses.COMPLETED,
        )

    def test_handing_the_lead_over_moves_the_right(self):
        self.assertEqual(self.set_lead(self.second).status_code, status.HTTP_200_OK)
        response = self.submit_contribution(self.second)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.complete("done", user=self.lead).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(self.reject(self.lead).status_code, status.HTTP_403_FORBIDDEN)
        response = self.complete("done", user=self.second)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_what_the_page_offers_follows_the_lead(self):
        self.submit_lead_report()
        instance = self.instance(self.consolidated)
        self.assertTrue(workflow_service.is_ready_to_complete(self.lead, instance))
        self.assertFalse(workflow_service.is_ready_to_complete(self.second, instance))
        self.assertTrue(
            proposal_permissions.user_can_answer_step_checklist(
                self.lead, self.proposal, self.consolidated
            )
        )
        self.assertFalse(
            proposal_permissions.user_can_answer_step_checklist(
                self.second, self.proposal, self.consolidated
            )
        )
        # Both still work on the step and read what it reads.
        self.assertTrue(
            proposal_permissions.user_holds_step_role(
                self.second, self.proposal, self.consolidated
            )
        )
        self.assertEqual(
            set(
                notification_rules._responsible_role_users(
                    self.consolidated, self.proposal
                )
            ),
            {self.lead},
        )

    def test_only_the_lead_releases_the_report(self):
        self.submit_lead_report()
        self.assertEqual(
            self.complete("done", user=self.lead).status_code, status.HTTP_200_OK
        )
        instance = self.instance(self.consolidated)
        self.assertTrue(step_inputs.can_release(self.lead, instance))
        self.assertFalse(step_inputs.can_release(self.second, instance))

    def test_every_holder_completes_a_step_every_holder_writes(self):
        individual = self.instance(self.individual)
        for user in (self.lead, self.second):
            self.assertTrue(
                proposal_permissions.user_completes_step(
                    user, self.proposal, self.individual
                )
            )
            self.assertTrue(step_inputs.can_release(user, individual))


class NoLeadTest(LeadOnlyStepMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.instance_ = self.reach_consolidated_step(mark_lead=False)

    def test_no_slot_is_made_and_the_step_waits(self):
        self.assertEqual(self.slots(), {})
        message = self.awaiting_lead()
        self.assertIn("Only the lead of the role 'Rapporteur'", message)
        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("mark one holder as the lead", str(response.data))

    def test_nobody_may_contribute_until_a_lead_is_marked(self):
        for user in (self.lead, self.second):
            self.assertFalse(contributions.may_contribute(user, self.instance_))
            self.assertEqual(
                self.submit_contribution(user).status_code,
                status.HTTP_400_BAD_REQUEST,
            )
        self.assertEqual(self.set_lead(self.second).status_code, status.HTTP_200_OK)
        self.assertEqual(self.slots(), {self.second: Statuses.PENDING})
        self.assertIsNone(self.awaiting_lead())

    def test_a_lead_awaiting_coi_confirmation_is_named_as_such(self):
        models.ProposalRoleAssignment.objects.filter(
            proposal=self.proposal, user=self.lead
        ).update(is_lead=True, coi_confirmation_required=True)
        self.assertIn("conflict of interest", self.awaiting_lead())


class LeadOnlyConfigTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.list_url = factories.CallWorkflowStepFactory.get_list_url(self.call)

    def create(self, **values):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            self.list_url,
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Consolidated report",
                "outcome_set": "done",
                "contributors_lead_only": True,
                **values,
            },
        )

    def test_lead_only_on_a_role_with_a_lead(self):
        response = self.create(contributor_source=f"proposal_role:{self.role.pk}")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data["contributors_lead_only"])
        self.assertEqual(
            response.data["contributor_proposal_role_uuid"], self.role.uuid.hex
        )

    def test_refused_on_a_role_without_a_lead(self):
        role = self.any_user_role(allows_lead=False)
        response = self.create(contributor_source=f"proposal_role:{role.pk}")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("contributors_lead_only", response.data)

    def test_refused_on_a_call_role_source(self):
        response = self.create(contributor_source="call_role:panel_member")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("contributors_lead_only", response.data)

    def test_refused_without_contributors(self):
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("contributors_lead_only", response.data)

    def test_the_role_keeps_its_lead_while_a_step_relies_on_it(self):
        self.create(contributor_source=f"proposal_role:{self.role.pk}")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            "http://testserver/api/proposal-protected-calls/"
            f"{self.call.uuid.hex}/proposal_roles/{self.role.uuid.hex}/",
            {"allows_lead": False},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.role.refresh_from_db()
        self.assertTrue(self.role.allows_lead)

    def test_the_kinds_endpoint_offers_it_on_assessment_steps(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/proposal-protected-calls/workflow_step_kinds/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kinds = {kind["id"]: kind for kind in response.data}
        self.assertIn(
            "contributors_lead_only", kinds[StepKinds.ASSESSMENT]["config_fields"]
        )
        self.assertNotIn(
            "contributors_lead_only", kinds[StepKinds.DECISION]["config_fields"]
        )


class ContributionCapacityTest(EvaluatorRoleTestMixin, test.APITestCase):
    """A step's minimum of contributions stays within what its evaluator
    role's holders can give, or a proposal would wait at it for good."""

    def setUp(self):
        super().setUp()
        self.list_url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        self.client.force_authenticate(self.fixture.staff)

    def create(self, **values):
        return self.client.post(
            self.list_url,
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Report",
                "outcome_set": "done",
                "contributor_source": f"proposal_role:{self.role.pk}",
                **values,
            },
        )

    def patch_role(self, **values):
        return self.client.patch(
            "http://testserver/api/proposal-protected-calls/"
            f"{self.call.uuid.hex}/proposal_roles/{self.role.uuid.hex}/",
            {"name": self.role.name, **values},
        )

    def test_a_minimum_every_holder_can_meet(self):
        response = self.create(min_contributions=2)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_more_contributions_than_holders_are_refused(self):
        response = self.create(min_contributions=3)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at most 2 holder", str(response.data["min_contributions"]))

    def test_more_than_one_contribution_from_the_lead_is_refused(self):
        response = self.create(min_contributions=2, contributors_lead_only=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only the lead", str(response.data["min_contributions"]))

    def test_a_role_without_a_maximum_takes_any_minimum(self):
        self.role.max_holders = None
        self.role.save()
        response = self.create(min_contributions=5)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_the_role_keeps_the_holders_a_step_needs(self):
        self.assertEqual(self.create(min_contributions=2).status_code, 201)
        response = self.patch_role(min_holders=1, max_holders=1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("'Report' needs 2", str(response.data["max_holders"]))
        self.role.refresh_from_db()
        self.assertEqual(self.role.max_holders, 2)

    def test_a_lead_only_step_lets_the_role_shrink_to_one(self):
        response = self.create(min_contributions=1, contributors_lead_only=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.patch_role(min_holders=1, max_holders=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class RoleAssignedFirstTest(EvaluatorRoleTestMixin, test.APITestCase):
    """A step its evaluator role works on comes after the step assigning the
    role, or a proposal reaches it before anyone can hold the role."""

    def setUp(self):
        super().setUp()
        self.list_url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        self.client.force_authenticate(self.fixture.staff)

    def report(self, **values):
        return self.client.post(
            self.list_url,
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Report",
                "outcome_set": "done",
                "contributor_source": f"proposal_role:{self.role.pk}",
                **values,
            },
        )

    def patch(self, step, payload):
        return self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step), payload
        )

    def test_a_step_after_the_assignment_is_accepted(self):
        response = self.report()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_step_before_the_assignment_is_refused(self):
        response = self.report(order=self.step.order)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "'Rapporteur assignment' assigns only after it",
            str(response.data["order"]),
        )
        self.assertFalse(self.call.workflow_steps.filter(name="Report").exists())

    def test_a_step_completed_by_the_role_counts_too(self):
        response = self.report(
            order=self.step.order,
            contributor_source="none",
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_moving_the_assignment_after_the_step_is_refused(self):
        report = self.call.workflow_steps.get(uuid=self.report().data["uuid"])
        response = self.patch(self.step, {"order": report.order + 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.step.refresh_from_db()
        self.assertLess(self.step.order, report.order)

    def test_enabling_a_late_assignment_is_refused(self):
        self.patch(self.step, {"is_enabled": False})
        response = self.report(order=self.step.order)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.patch(self.step, {"is_enabled": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_role_no_step_assigns_is_left_to_the_call_managers(self):
        self.step.assigns_roles.set([])
        response = self.report(order=self.step.order)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_import_refuses_it(self):
        self.report()
        document, _ = call_transfer.export_call(self.call)
        assignment = next(
            s for s in document["workflow_steps"] if s["key"] == "rapporteur-assignment"
        )
        # After every other step, the report among them.
        assignment["order"] = 999
        with self.assertRaises(serializers.ValidationError) as caught:
            call_transfer.import_call(document, self.call.manager, self.fixture.staff)
        self.assertIn("assigns only after it", str(caught.exception.detail))


class LeadOnlyLockAndCopyTest(LeadOnlyStepMixin, test.APITestCase):
    def test_locked_once_proposals_walk_the_call(self):
        self.submit()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.consolidated),
            {"contributors_lead_only": False},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("contributors_lead_only", response.data)
        self.consolidated.refresh_from_db()
        self.assertTrue(self.consolidated.contributors_lead_only)

    def assert_copied(self, call):
        role = call.proposal_roles.get(key="rapporteur")
        step = call.workflow_steps.get(key="consolidated-report")
        self.assertTrue(step.contributors_lead_only)
        self.assertEqual(step.contributor_source, f"proposal_role:{role.pk}")
        self.assertFalse(
            call.workflow_steps.get(key="individual-report").contributors_lead_only
        )

    def test_duplicate_call_copies_it(self):
        self.assert_copied(utils.duplicate_call(self.call, "Copy", self.fixture.staff))

    def test_export_and_import_carry_it(self):
        document, _ = call_transfer.export_call(self.call)
        exported = next(
            step
            for step in document["workflow_steps"]
            if step["key"] == "consolidated-report"
        )
        self.assertTrue(exported["contributors_lead_only"])
        call, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.fixture.staff
        )
        self.assertEqual(warnings, [])
        self.assert_copied(call)

    def test_import_refuses_it_on_a_role_without_a_lead(self):
        document, _ = call_transfer.export_call(self.call)
        for role in document["proposal_roles"]:
            role["allows_lead"] = False
        with self.assertRaises(serializers.ValidationError):
            call_transfer.import_call(document, self.call.manager, self.fixture.staff)


class LeadOnlyModelCleanTest(EvaluatorRoleTestMixin, test.APITestCase):
    def test_clean_names_the_role_without_a_lead(self):
        role = models.CallProposalRole.objects.create(
            call=self.call,
            key="external",
            name="External",
            eligible_from=ProposalRoleEligibility.ANY_USER,
        )
        step = models.CallWorkflowStep(
            call=self.call,
            key="report",
            kind=StepKinds.ASSESSMENT,
            name="Report",
            outcome_set="done",
            contributor_source=f"proposal_role:{role.pk}",
            contributors_lead_only=True,
        )
        with self.assertRaisesMessage(Exception, "'External' has no lead"):
            step.clean()
