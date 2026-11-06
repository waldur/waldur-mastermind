"""What a provider reads of a proposal requesting several offerings.

Someone who reads a proposal only as the manager of one of the offerings it
requests reads only what concerns their own offering: its requested
resources, its slot, its award and its provisioning. Never another
provider's request, amounts or outcome. Whoever reads the proposal in any
other capacity -- staff, support, the call team, its panel, the applicant
team -- reads all of it, as before.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models, tasks, workflow_service
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    OutcomeSets,
    ProposalRunStates,
    ProposalStates,
    WorkflowPhases,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class TwoProviderProposalBase(test.APITestCase):
    """A submitted proposal requesting partitions of two providers, A and B,
    at a technical assessment split per requested offering."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager

        models.CallWorkflowStep.objects.filter(
            call=self.call, key="administrative_check"
        ).update(is_enabled=False)
        self.call_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="technical_assessment"
        )
        self.call_step.is_enabled = True
        self.call_step.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
        self.call_step.per_requested_offering = True
        self.call_step.outcome_set = OutcomeSets.PER_OFFERING
        self.call_step.applicant_visible = True
        self.call_step.save()

        self.proposal = factories.ProposalFactory(
            round=self.fixture.round,
            state=ProposalStates.SUBMITTED,
            allocation_comment="A: half of it; B: refused",
        )
        self.applicant = fixtures.author_as_manager(self.proposal)
        (
            self.manager_a,
            self.offering_a,
            self.requested_a,
            self.resource_a,
        ) = self._request_offering({"cpu": 100})
        (
            self.manager_b,
            self.offering_b,
            self.requested_b,
            self.resource_b,
        ) = self._request_offering({"cpu": 400})

        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()
        self.instance = self.proposal.current_step

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.panel_member = self.fixture.panel_member

    def _request_offering(self, limits):
        manager = structure_factories.UserFactory()
        offering = marketplace_factories.OfferingFactory()
        offering.add_user(manager, OfferingRole.MANAGER)
        requested = factories.RequestedOfferingFactory(
            call=self.call, offering=offering, created_by=self.fixture.owner
        )
        resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=requested,
            resource=None,
            limits=limits,
        )
        return manager, offering, requested, resource

    def _url(self, action=None):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def _get(self, user, url, **params):
        self.client.force_authenticate(user)
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def _whole_readers(self):
        return {
            "staff": self.staff,
            "support": self.support,
            "call manager": self.call_manager,
            "applicant": self.applicant,
            "panel member": self.panel_member,
        }


class RequestedResourcesTest(TwoProviderProposalBase):
    def _offerings_listed(self, user):
        rows = self._get(user, self._url("resources"))
        return {row["requested_offering"]["offering_uuid"] for row in rows}

    def test_a_provider_lists_only_its_own_offerings_request(self):
        self.assertEqual(
            self._offerings_listed(self.manager_a), {self.offering_a.uuid.hex}
        )
        self.assertEqual(
            self._offerings_listed(self.manager_b), {self.offering_b.uuid.hex}
        )

    def test_the_proposal_says_whether_the_caller_reads_only_its_own_part(self):
        self.assertTrue(
            self._get(self.manager_a, self._url())["reads_own_offerings_only"]
        )
        for name, user in self._whole_readers().items():
            with self.subTest(name):
                self.assertFalse(
                    self._get(user, self._url())["reads_own_offerings_only"]
                )

    def test_everyone_else_lists_every_request(self):
        both = {self.offering_a.uuid.hex, self.offering_b.uuid.hex}
        for name, user in self._whole_readers().items():
            with self.subTest(name):
                self.assertEqual(self._offerings_listed(user), both)

    def test_another_providers_request_is_not_found(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.get(
            factories.RequestedResourceFactory.get_url(self.proposal, self.resource_b)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.get(
            factories.RequestedResourceFactory.get_url(self.proposal, self.resource_a)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limits"], {"cpu": 100})

    def test_the_call_manager_reads_another_providers_request(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            factories.RequestedResourceFactory.get_url(self.proposal, self.resource_b)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_provider_on_the_applicant_team_reads_every_request(self):
        self.proposal.add_user(self.manager_a, fixtures.ProposalRole.MEMBER)
        self.assertEqual(
            self._offerings_listed(self.manager_a),
            {self.offering_a.uuid.hex, self.offering_b.uuid.hex},
        )

    def test_a_provider_on_the_panel_reads_every_request(self):
        self.call.add_user(self.manager_a, fixtures.CallRole.PANEL_MEMBER)
        self.assertEqual(
            self._offerings_listed(self.manager_a),
            {self.offering_a.uuid.hex, self.offering_b.uuid.hex},
        )

    def test_my_requested_resources_list_only_a_providers_own(self):
        rows = self._get(
            self.manager_a,
            factories.RequestedResourceFactory.get_my_list_url(),
            include_closed="true",
        )
        self.assertEqual({row["uuid"] for row in rows}, {self.resource_a.uuid.hex})
        rows = self._get(
            self.applicant,
            factories.RequestedResourceFactory.get_my_list_url(),
            include_closed="true",
        )
        self.assertEqual(
            {row["uuid"] for row in rows},
            {self.resource_a.uuid.hex, self.resource_b.uuid.hex},
        )

    def test_provider_requested_resources_list_only_its_own(self):
        rows = self._get(
            self.manager_a, factories.RequestedResourceFactory.get_provider_list_url()
        )
        self.assertEqual({row["uuid"] for row in rows}, {self.resource_a.uuid.hex})

    def test_narrowing_costs_nothing_per_hidden_request(self):
        def queries():
            self.client.force_authenticate(self.manager_a)
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(self._url("resources"))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            return len(captured)

        queries()  # warm the content type and role caches
        before = queries()
        for _ in range(3):
            factories.RequestedResourceFactory(
                proposal=self.proposal,
                requested_offering=self.requested_b,
                resource=None,
                limits={"cpu": 1},
            )
        self.assertEqual(queries(), before)


class AwardTest(TwoProviderProposalBase):
    def setUp(self):
        super().setUp()
        self.awarded_a = models.AwardedResource.objects.create(
            proposal=self.proposal,
            requested_offering=self.requested_a,
            plan=self.requested_a.plan,
            limits={"cpu": 50},
        )
        self.awarded_b = models.AwardedResource.objects.create(
            proposal=self.proposal,
            requested_offering=self.requested_b,
            plan=self.requested_b.plan,
            limits={"cpu": 10},
        )

    def _awarded(self, user):
        return {row["uuid"] for row in self._get(user, self._url("awarded_resources"))}

    def test_a_provider_reads_only_its_own_part_of_the_award(self):
        self.assertEqual(self._awarded(self.manager_a), {self.awarded_a.uuid.hex})
        self.assertEqual(self._awarded(self.manager_b), {self.awarded_b.uuid.hex})

    def test_the_call_team_reads_the_whole_award(self):
        both = {self.awarded_a.uuid.hex, self.awarded_b.uuid.hex}
        for user in (self.call_manager, self.staff, self.support):
            self.assertEqual(self._awarded(user), both)

    def test_another_providers_award_item_is_not_found(self):
        self.client.force_authenticate(self.manager_a)
        url = self._url("awarded_resources") + self.awarded_b.uuid.hex + "/"
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        url = self._url("awarded_resources") + self.awarded_a.uuid.hex + "/"
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    def test_the_allocation_comment_is_not_a_providers(self):
        self.assertIsNone(self._get(self.manager_a, self._url())["allocation_comment"])
        rows = self._get(self.manager_a, factories.ProposalFactory.get_list_url())
        self.assertEqual([row["allocation_comment"] for row in rows], [None])
        for name, user in self._whole_readers().items():
            with self.subTest(name):
                self.assertEqual(
                    self._get(user, self._url())["allocation_comment"],
                    "A: half of it; B: refused",
                )


class ExtensionRunTest(TwoProviderProposalBase):
    def setUp(self):
        super().setUp()
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save(update_fields=["state"])
        self.provisioned_a = marketplace_factories.ResourceFactory(
            offering=self.offering_a
        )
        self.provisioned_b = marketplace_factories.ResourceFactory(
            offering=self.offering_b
        )
        a, b = self.provisioned_a.uuid.hex, self.provisioned_b.uuid.hex
        self.run = models.ProposalRun.objects.create(
            proposal=self.proposal,
            phase=WorkflowPhases.EXTENSION,
            state=ProposalRunStates.APPROVED,
            requested_limits={a: {"cpu": 200}, b: {"cpu": 800}},
            granted_limits={a: {"cpu": 150}, b: {"cpu": 0}},
            applied_changes={
                "project_end_date": None,
                "resource_end_dates": [],
                "orders": [{"resource_uuid": a, "order_uuid": "1" * 32}],
                "skipped": [{"resource_uuid": b, "reason": "Refused."}],
            },
        )

    def test_a_provider_reads_only_its_own_resources_limits(self):
        (row,) = self._get(self.manager_a, self._url("runs"))
        a = self.provisioned_a.uuid.hex
        self.assertEqual(row["requested_limits"], {a: {"cpu": 200}})
        self.assertEqual(row["granted_limits"], {a: {"cpu": 150}})
        self.assertEqual(len(row["applied_changes"]["orders"]), 1)
        self.assertEqual(row["applied_changes"]["skipped"], [])

    def test_the_call_manager_reads_every_resources_limits(self):
        (row,) = self._get(self.call_manager, self._url("runs"))
        self.assertEqual(len(row["requested_limits"]), 2)
        self.assertEqual(len(row["applied_changes"]["skipped"]), 1)


class AssessmentTest(TwoProviderProposalBase):
    def _submit(self, user, slot, comment):
        self.client.force_authenticate(user)
        response = self.client.post(
            self._url("submit_contribution"),
            {
                "contribution_uuid": slot.uuid.hex,
                "outcome": "accepted",
                "comment": comment,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def setUp(self):
        super().setUp()
        self.slot_a = self.instance.contributions.get(
            requested_offering=self.requested_a
        )
        self.slot_b = self.instance.contributions.get(
            requested_offering=self.requested_b
        )
        self._submit(self.manager_a, self.slot_a, "A fits")
        self._submit(self.manager_b, self.slot_b, "B fits")
        self.proposal.refresh_from_db()
        self.instance.refresh_from_db()

    def test_a_provider_reads_only_its_own_slot(self):
        for action in ("contributions", "offering_assessments"):
            with self.subTest(action):
                data = str(self._get(self.manager_a, self._url(action)))
                self.assertIn("A fits", data)
                self.assertNotIn("B fits", data)

    def test_the_call_manager_reads_every_slot(self):
        for action in ("contributions", "offering_assessments"):
            with self.subTest(action):
                data = str(self._get(self.call_manager, self._url(action)))
                self.assertIn("A fits", data)
                self.assertIn("B fits", data)

    def _complete_with_reason(self):
        models.ProposalWorkflowStepInstance.objects.filter(pk=self.instance.pk).update(
            outcome="approved",
            outcome_reason="A fits; B lacks GPUs",
            completed_at=timezone.now(),
        )
        self.instance.refresh_from_db()

    def _step_reason(self, user):
        rows = self._get(user, self._url("workflow_states"))
        (row,) = [row for row in rows if row["uuid"] == self.instance.uuid.hex]
        return row["outcome_reason"]

    def test_the_step_reason_speaking_to_every_slot_is_not_a_providers(self):
        self._complete_with_reason()
        self.assertEqual(self._step_reason(self.manager_a), "")
        self.assertEqual(self._step_reason(self.call_manager), "A fits; B lacks GPUs")

    def test_mail_about_the_step_gives_a_provider_no_reason(self):
        self._complete_with_reason()
        self.assertEqual(
            tasks._step_outcome_for(self.manager_a, self.instance), ("approved", "")
        )
        self.assertEqual(
            tasks._step_outcome_for(self.call_manager, self.instance),
            ("approved", "A fits; B lacks GPUs"),
        )
