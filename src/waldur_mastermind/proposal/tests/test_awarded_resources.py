"""What the allocation decision awards, as opposed to what was requested.

The call manager may award amounts and offerings that differ from the request
while the allocation decision is open; allocation provisions the award, and the
request stays as it was submitted.
"""

import datetime
from importlib import import_module

from dateutil.relativedelta import relativedelta
from django.apps import apps as django_apps
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    LimitPeriods,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models, utils, workflow_service
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    RequestedOfferingStates,
    ResultsPublication,
    RoundLifecycleStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures

backfill_migration = import_module(
    "waldur_mastermind.proposal.migrations.0089_awardedresource"
)


def _limit_offering(customer=None, min_value=1, max_value=1000):
    offering = marketplace_factories.OfferingFactory(
        type=BASIC_OFFERING,
        **({"customer": customer} if customer else {}),
    )
    component = marketplace_factories.OfferingComponentFactory(
        offering=offering,
        type="cpu",
        billing_type=BillingTypes.LIMIT,
        min_value=min_value,
        max_value=max_value,
    )
    plan = marketplace_factories.PlanFactory(offering=offering)
    marketplace_factories.PlanComponentFactory(
        plan=plan, component=component, price=1, amount=0
    )
    return offering, plan


class AwardedResourcesBase(test.APITestCase):
    """A proposal whose next step is the allocation decision."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.project = None
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()
        # Only the requests made below.
        self.proposal.requestedresource_set.all().delete()

        self.offering, self.plan = _limit_offering()
        self.requested_offering = factories.RequestedOfferingFactory(
            call=self.call, offering=self.offering, plan=self.plan
        )
        self.other_offering, self.other_plan = _limit_offering()
        self.other_requested_offering = factories.RequestedOfferingFactory(
            call=self.call, offering=self.other_offering, plan=self.other_plan
        )
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            resource=None,
            limits={"cpu": 100},
            attributes={"name": "requested"},
            description="What we need",
        )

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")
        self.admin_instance = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.call_manager = self.fixture.call_manager
        self.applicant = self.proposal.created_by

    def _start_allocation(self):
        workflow_service.complete_step(
            proposal=self.proposal,
            current_instance=self.admin_instance,
            outcome=WorkflowStepOutcomes.ELIGIBLE,
            outcome_reason="",
            completed_by=self.call_manager,
        )
        self.proposal.refresh_from_db()

    def _allocation_instance(self):
        return self.proposal.workflow_step_instances.get(step="allocation_decision")

    def _decide(self, user=None, outcome=WorkflowStepOutcomes.APPROVED):
        self.client.force_authenticate(user or self.call_manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="complete_workflow_step"
            ),
            {"step_uuid": self._allocation_instance().uuid.hex, "outcome": outcome},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()

    def _list_url(self):
        return factories.ProposalFactory.get_url(
            self.proposal, action="awarded_resources"
        )

    def _detail_url(self, awarded):
        return self._list_url() + awarded.uuid.hex + "/"

    def _awarded(self):
        return self.proposal.awarded_resources.get()


class PrefillTest(AwardedResourcesBase):
    def test_nothing_is_awarded_before_the_allocation_decision(self):
        self.assertFalse(self.proposal.awarded_resources.exists())
        self.assertIsNone(self.proposal.awarded_resources_prefilled_at)

    def test_the_award_is_prefilled_from_the_request_when_allocation_starts(self):
        self._start_allocation()

        awarded = self._awarded()
        self.assertEqual(awarded.requested_resource, self.requested_resource)
        self.assertEqual(awarded.requested_offering, self.requested_offering)
        # Not copied: an award nobody changed the plan of follows the call's.
        self.assertIsNone(awarded.plan)
        self.assertEqual(awarded.effective_plan, self.plan)
        self.assertEqual(awarded.limits, {"cpu": 100})
        self.assertEqual(awarded.attributes, {"name": "requested"})
        self.assertEqual(awarded.description, "What we need")
        self.assertIsNotNone(self.proposal.awarded_resources_prefilled_at)

    def test_prefilling_twice_does_not_duplicate(self):
        self._start_allocation()
        utils.prefill_awarded_resources(self.proposal)

        self.assertEqual(self.proposal.awarded_resources.count(), 1)

    def test_an_award_emptied_by_the_manager_is_not_prefilled_again(self):
        self._start_allocation()
        self._awarded().delete()

        utils.prefill_awarded_resources(self.proposal)

        self.assertFalse(self.proposal.awarded_resources.exists())

    def test_allocation_decision_as_first_step_is_prefilled_too(self):
        self.proposal.workflow_step_instances.all().delete()
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()
        models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

        workflow_service.activate_first_step(self.proposal)

        self.assertEqual(self.proposal.awarded_resources.count(), 1)


class EditAwardTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        self._start_allocation()
        self.awarded = self._awarded()

    def _patch_json(self, payload, user=None):
        self.client.force_authenticate(user or self.call_manager)
        return self.client.patch(self._detail_url(self.awarded), payload, format="json")

    def test_call_manager_lists_the_award(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["uuid"], self.awarded.uuid.hex)
        self.assertEqual(
            str(row["requested_resource"]), self.requested_resource.uuid.hex
        )
        self.assertEqual(row["limits"], {"cpu": 100})
        self.assertEqual(
            str(row["requested_offering"]["uuid"]), self.requested_offering.uuid.hex
        )
        self.assertEqual(str(row["plan"]), self.plan.uuid.hex)

    def test_call_manager_changes_the_awarded_amount(self):
        response = self._patch_json({"limits": {"cpu": 40}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.limits, {"cpu": 40})
        # The request is the historical record and stays as submitted.
        self.requested_resource.refresh_from_db()
        self.assertEqual(self.requested_resource.limits, {"cpu": 100})

    def test_limits_outside_the_component_bounds_are_refused(self):
        response = self._patch_json({"limits": {"cpu": 5000}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)

    def test_limits_for_an_unknown_component_are_refused(self):
        response = self._patch_json({"limits": {"gpu": 1}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)

    def test_the_item_moves_to_another_accepted_offering_of_the_call(self):
        response = self._patch_json(
            {
                "requested_offering_uuid": self.other_requested_offering.uuid.hex,
                "limits": {"cpu": 10},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.requested_offering, self.other_requested_offering)
        # The target's own plan, since the old one belongs to another offering.
        self.assertEqual(self.awarded.effective_plan, self.other_plan)
        self.assertEqual(str(response.data["plan"]), self.other_plan.uuid.hex)
        self.requested_resource.refresh_from_db()
        self.assertEqual(
            self.requested_resource.requested_offering, self.requested_offering
        )

    def test_moving_to_an_offering_the_provider_has_not_accepted_is_refused(self):
        self.other_requested_offering.state = RequestedOfferingStates.REQUESTED
        self.other_requested_offering.save()

        response = self._patch_json(
            {"requested_offering_uuid": self.other_requested_offering.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_moving_to_an_offering_of_another_call_is_refused(self):
        foreign = factories.RequestedOfferingFactory(
            call=self.fixture.new_call, offering=self.other_offering
        )

        response = self._patch_json({"requested_offering_uuid": foreign.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_plan_of_another_offering_is_refused(self):
        response = self._patch_json({"plan": self.other_plan.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plan", response.data)

    def test_an_archived_plan_is_refused(self):
        archived = marketplace_factories.PlanFactory(
            offering=self.offering, archived=True
        )

        response = self._patch_json({"plan": archived.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plan", response.data)

    def test_another_plan_of_the_same_offering_is_accepted(self):
        second_plan = marketplace_factories.PlanFactory(offering=self.offering)

        response = self._patch_json({"plan": second_plan.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.plan, second_plan)

    def test_call_manager_adds_an_item(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._list_url(),
            {
                "requested_offering_uuid": self.other_requested_offering.uuid.hex,
                "limits": {"cpu": 7},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        added = self.proposal.awarded_resources.get(uuid=response.data["uuid"])
        self.assertIsNone(added.requested_resource)
        self.assertEqual(added.effective_plan, self.other_plan)
        self.assertEqual(added.limits, {"cpu": 7})
        self.assertEqual(added.created_by, self.call_manager)
        self.assertEqual(self.proposal.requestedresource_set.count(), 1)

    def test_adding_without_limits_is_refused(self):
        # Allocation would provision a resource with no quota at all.
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._list_url(),
            {"requested_offering_uuid": self.other_requested_offering.uuid.hex},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)
        self.assertEqual(self.proposal.awarded_resources.count(), 1)

    def test_emptying_the_limits_is_refused(self):
        response = self._patch_json({"limits": {}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.limits, {"cpu": 100})

    def test_an_offering_with_nothing_to_ask_for_needs_no_limits(self):
        offering = marketplace_factories.OfferingFactory(type=BASIC_OFFERING)
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering, type="seat", billing_type=BillingTypes.FIXED
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        marketplace_factories.PlanComponentFactory(
            plan=plan, component=component, price=1, amount=1
        )
        entry = factories.RequestedOfferingFactory(
            call=self.call, offering=offering, plan=plan
        )

        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._list_url(),
            {"requested_offering_uuid": entry.uuid.hex},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_adding_without_an_offering_is_refused(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._list_url(), {"limits": {"cpu": 7}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_call_manager_removes_an_item(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.delete(self._detail_url(self.awarded))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self.proposal.awarded_resources.exists())
        self.assertTrue(
            models.RequestedResource.objects.filter(
                pk=self.requested_resource.pk
            ).exists()
        )

    def test_staff_may_edit(self):
        response = self._patch_json({"limits": {"cpu": 40}}, user=self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_applicant_may_not_edit(self):
        response = self._patch_json({"limits": {"cpu": 999}}, user=self.applicant)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_reviewer_may_not_edit(self):
        response = self._patch_json(
            {"limits": {"cpu": 999}}, user=self.fixture.reviewer_1
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_offering_manager_may_not_edit(self):
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        response = self._patch_json({"limits": {"cpu": 999}}, user=manager)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_edit_locks_the_decision_step_it_checks(self):
        # The expiry task locks only the step instance: an edit that checked
        # the step without locking it could commit after the step expired.
        with CaptureQueriesContext(connection) as queries:
            response = self._patch_json({"limits": {"cpu": 40}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        table = models.ProposalWorkflowStepInstance._meta.db_table
        self.assertTrue(
            any(
                "FOR UPDATE" in query["sql"] and f'FROM "{table}"' in query["sql"]
                for query in queries.captured_queries
            ),
            "the allocation decision step is not locked by the edit",
        )

    def test_an_expired_decision_refuses_the_edit(self):
        instance = self._allocation_instance()
        instance.status = WorkflowStepInstanceStatuses.EXPIRED
        instance.save()

        response = self._patch_json({"limits": {"cpu": 40}})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_award_is_frozen_once_the_decision_is_taken(self):
        instance = self._allocation_instance()
        instance.status = WorkflowStepInstanceStatuses.COMPLETED
        instance.outcome = WorkflowStepOutcomes.APPROVED
        instance.save()

        response = self._patch_json({"limits": {"cpu": 40}})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.client.force_authenticate(self.call_manager)
        response = self.client.delete(self._detail_url(self.awarded))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        response = self.client.post(
            self._list_url(),
            {"requested_offering_uuid": self.requested_offering.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_award_is_not_editable_before_allocation_starts(self):
        self.proposal.awarded_resources.all().delete()
        instance = self._allocation_instance()
        instance.status = WorkflowStepInstanceStatuses.PENDING
        instance.save()

        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._list_url(),
            {"requested_offering_uuid": self.requested_offering.uuid.hex},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_proposal_at_the_step_without_a_prefill_is_prefilled_on_read(self):
        # Seeded by hand in older tests and presets: the step is active but
        # activation never ran, so the editor would otherwise start empty.
        self.proposal.awarded_resources.all().delete()
        self.proposal.awarded_resources_prefilled_at = None
        self.proposal.save()

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


class AllocateFromAwardTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        self._start_allocation()
        self.awarded = self._awarded()

    def _provisioned(self):
        self.proposal.refresh_from_db()
        return marketplace_models.Resource.objects.filter(
            project=self.proposal.project
        ).order_by("id")

    def test_the_awarded_amount_is_provisioned(self):
        self.awarded.limits = {"cpu": 40}
        self.awarded.save()

        self._decide()

        resource = self._provisioned().get()
        self.assertEqual(resource.limits, {"cpu": 40})
        order = marketplace_models.Order.objects.get(resource=resource)
        self.assertEqual(order.limits, {"cpu": 40})
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.resource, resource)
        self.requested_resource.refresh_from_db()
        self.assertEqual(self.requested_resource.limits, {"cpu": 100})
        self.assertEqual(self.requested_resource.resource, resource)

    def test_the_decision_cannot_approve_an_award_without_amounts(self):
        # Written around the API, as an award prefilled from an older request
        # or a preset can be.
        self.awarded.limits = {}
        self.awarded.save()

        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="complete_workflow_step"
            ),
            {
                "step_uuid": self._allocation_instance().uuid.hex,
                "outcome": WorkflowStepOutcomes.APPROVED,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(self.offering.name, str(response.data))
        self.assertEqual(
            self._allocation_instance().status, WorkflowStepInstanceStatuses.ACTIVE
        )
        self.proposal.refresh_from_db()
        self.assertIsNone(self.proposal.project)

    def test_the_decision_may_still_decline_an_award_without_amounts(self):
        self.awarded.limits = {}
        self.awarded.save()

        self._decide(outcome=WorkflowStepOutcomes.DECLINED)

        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)

    def test_an_empty_award_on_a_withdrawn_offering_does_not_block_the_decision(
        self,
    ):
        # Allocation skips it, so it provisions nothing either way.
        self.requested_offering.state = RequestedOfferingStates.CANCELED
        self.requested_offering.save()
        self.awarded.limits = {}
        self.awarded.save()

        self._decide()

        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)

    def test_the_awarded_plan_falls_back_to_the_call_entry_when_deleted(self):
        second_plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.awarded.plan = second_plan
        self.awarded.save()

        second_plan.delete()
        self.awarded.refresh_from_db()
        self.assertIsNone(self.awarded.plan)

        self._decide()

        self.assertEqual(self._provisioned().get().plan, self.plan)

    def test_a_moved_item_is_provisioned_on_the_new_offering(self):
        self.awarded.requested_offering = self.other_requested_offering
        self.awarded.plan = self.other_plan
        self.awarded.save()

        self._decide()

        resource = self._provisioned().get()
        self.assertEqual(resource.offering, self.other_offering)
        self.assertEqual(resource.plan, self.other_plan)
        self.assertTrue(resource.name.endswith(f" - {self.other_offering.name}"))

    def test_a_removed_item_is_not_provisioned(self):
        self.awarded.delete()

        self._decide()

        self.assertFalse(self._provisioned().exists())
        self.assertIsNotNone(self.proposal.project)

    def test_an_added_item_is_provisioned(self):
        models.AwardedResource.objects.create(
            proposal=self.proposal,
            requested_offering=self.other_requested_offering,
            plan=self.other_plan,
            limits={"cpu": 3},
        )

        self._decide()

        offerings = [r.offering for r in self._provisioned()]
        self.assertEqual(offerings, [self.offering, self.other_offering])

    def test_an_award_on_an_offering_no_longer_accepted_is_skipped(self):
        # As with the request: an offering the provider withdrew is not
        # provisioned.
        self.requested_offering.state = RequestedOfferingStates.CANCELED
        self.requested_offering.save()

        self._decide()

        self.assertFalse(self._provisioned().exists())

    def test_the_purchase_order_travels_only_with_the_requested_offering(self):
        self.requested_resource.purchase_order_reference = "PO-1"
        self.requested_resource.save()
        self.awarded.requested_offering = self.other_requested_offering
        self.awarded.plan = self.other_plan
        self.awarded.save()

        self._decide()

        order = marketplace_models.Order.objects.get(resource=self._provisioned().get())
        self.assertFalse(order.request_comment)


class UneditedAwardProvisionsAsRequestedTest(test.APITestCase):
    """An award nobody edited provisions exactly what the request would have.

    The expectation is built from the request alone, with the helpers
    allocation used before awards existed, so any drift between the award and
    the request it was prefilled from shows up here.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.project = None
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        self.proposal.requestedresource_set.all().delete()

        self.prepaid_offering = marketplace_factories.OfferingFactory(
            type=BASIC_OFFERING
        )
        cpu = marketplace_factories.OfferingComponentFactory(
            offering=self.prepaid_offering,
            type="cpu_hours",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )
        self.prepaid_plan = marketplace_factories.PlanFactory(
            offering=self.prepaid_offering, unit="month"
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.prepaid_plan, component=cpu, price=2, amount=0
        )
        self.prepaid_entry = factories.RequestedOfferingFactory(
            call=self.fixture.call,
            offering=self.prepaid_offering,
            plan=self.prepaid_plan,
        )
        self.limit_offering, limit_plan = _limit_offering()
        self.limit_entry = factories.RequestedOfferingFactory(
            call=self.fixture.call, offering=self.limit_offering, plan=limit_plan
        )
        withdrawn_offering, withdrawn_plan = _limit_offering()
        withdrawn_entry = factories.RequestedOfferingFactory(
            call=self.fixture.call,
            offering=withdrawn_offering,
            plan=withdrawn_plan,
            state=RequestedOfferingStates.CANCELED,
        )

        legacy_end = (datetime.date.today() + relativedelta(months=4)).isoformat()
        self.requests = [
            # Two requests for one offering, numbered in request order.
            self._request(
                self.prepaid_entry,
                {"cpu_hours": 100},
                {"prepaid_duration_months": 6},
            ),
            self._request(
                self.prepaid_entry,
                {"cpu_hours": 50},
                {"end_date": legacy_end},
                age_days=20,
            ),
            self._request(
                self.limit_entry,
                {"cpu": 10},
                {"name": "x"},
                purchase_order_reference="PO-42",
            ),
            self._request(withdrawn_entry, {"cpu": 1}, {}),
        ]
        self.requests[2].attachment.save("po.pdf", ContentFile(b"%PDF-1.4"))

    def _request(
        self, entry, limits, attributes, age_days=0, purchase_order_reference=""
    ):
        requested = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=entry,
            resource=None,
            limits=limits,
            attributes=attributes,
            purchase_order_reference=purchase_order_reference,
        )
        if age_days:
            requested.created -= datetime.timedelta(days=age_days)
            requested.save(update_fields=["created"])
        return requested

    def _expected(self, project):
        """What allocation made of the request before awards existed."""
        today = datetime.date.today()
        accepted = [
            r
            for r in sorted(self.requests, key=lambda r: (r.created, r.id))
            if r.requested_offering.state == RequestedOfferingStates.ACCEPTED
        ]
        counts = {}
        for r in accepted:
            counts[r.requested_offering.offering_id] = (
                counts.get(r.requested_offering.offering_id, 0) + 1
            )
        seen = {}
        expected = []
        for r in accepted:
            offering = r.requested_offering.offering
            seen[offering.id] = seen.get(offering.id, 0) + 1
            index = seen[offering.id] if counts[offering.id] > 1 else None
            expected.append(
                {
                    "offering": offering.id,
                    "plan": r.requested_offering.plan_id,
                    "limits": r.limits,
                    "attributes": r.attributes,
                    "name": utils._allocated_resource_name(
                        project.name, offering.name, index
                    ),
                    "end_date": utils._requested_end_date(r, project, today),
                    "request_comment": r.purchase_order_reference or None,
                    "attachment": r.attachment.name if r.attachment else "",
                }
            )
        return expected

    def _actual(self, project):
        actual = []
        for resource in marketplace_models.Resource.objects.filter(
            project=project
        ).order_by("id"):
            order = marketplace_models.Order.objects.get(resource=resource)
            self.assertEqual(order.offering_id, resource.offering_id)
            self.assertEqual(order.plan_id, resource.plan_id)
            self.assertEqual(order.limits, resource.limits)
            self.assertEqual(order.attributes, resource.attributes)
            self.assertEqual(order.consumer_reviewed_by, self.fixture.staff)
            actual.append(
                {
                    "offering": resource.offering_id,
                    "plan": resource.plan_id,
                    "limits": resource.limits,
                    "attributes": resource.attributes,
                    "name": resource.name,
                    "end_date": resource.end_date,
                    "request_comment": order.request_comment,
                    "attachment": order.attachment.name or "",
                }
            )
        return actual

    def _assert_provisioned_as_requested(self):
        for r in self.requests:
            r.requested_offering.refresh_from_db()
        snapshot = [
            (r.requested_offering_id, r.limits, r.attributes) for r in self.requests
        ]

        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)

        self.proposal.refresh_from_db()
        project = self.proposal.project
        self.assertEqual(self._actual(project), self._expected(project))
        # The longest requested subscription still sets the project's length.
        self.assertEqual(
            project.end_date,
            datetime.date.today() + relativedelta(months=6),
        )
        for r in self.requests:
            r.refresh_from_db()
        self.assertEqual(
            [(r.requested_offering_id, r.limits, r.attributes) for r in self.requests],
            snapshot,
        )
        # Each request still points at the resource that fulfilled it.
        self.assertEqual(
            [r.resource is not None for r in self.requests],
            [True, True, True, False],
        )

    def test_prefilled_when_allocation_started(self):
        utils.prefill_awarded_resources(self.proposal)
        self.assertEqual(self.proposal.awarded_resources.count(), 4)

        self._assert_provisioned_as_requested()

    def test_the_call_entry_plan_changed_after_the_prefill_is_provisioned(self):
        # What allocation provisioned before awards existed: the call entry's
        # plan at allocation, not at the moment the decision started.
        utils.prefill_awarded_resources(self.proposal)
        new_plan = marketplace_factories.PlanFactory(offering=self.limit_offering)
        marketplace_factories.PlanComponentFactory(
            plan=new_plan,
            component=self.limit_offering.components.get(),
            price=3,
            amount=0,
        )
        self.limit_entry.plan = new_plan
        self.limit_entry.save()

        self._assert_provisioned_as_requested()

        self.assertTrue(
            marketplace_models.Resource.objects.filter(
                project=self.proposal.project, plan=new_plan
            ).exists()
        )

    def test_prefilled_at_allocation_for_a_proposal_that_never_had_one(self):
        self._assert_provisioned_as_requested()

        self.assertEqual(self.proposal.awarded_resources.count(), 4)
        self.assertEqual(
            self.proposal.awarded_resources.filter(resource__isnull=False).count(), 3
        )


class AwardVisibilityTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        self._start_allocation()

    def _get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self._list_url())

    def test_the_applicant_does_not_see_the_award_being_drafted(self):
        response = self._get(self.applicant)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_applicant_sees_the_award_once_allocated(self):
        self._decide()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)

        response = self._get(self.applicant)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_the_applicant_sees_the_award_they_are_asked_to_accept(self):
        factories.CallWorkflowStepFactory(
            call=self.call, step="award_response", is_enabled=True
        )
        models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="award_response",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

        self._decide()
        self.assertEqual(self.proposal.workflow_step, "award_response")

        response = self._get(self.applicant)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_decision_parked_for_manual_advance_follows_applicant_visibility(self):
        models.CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).update(transition_mode="manual", applicant_visible=False)
        self._decide()

        self.assertEqual(
            self._get(self.applicant).status_code, status.HTTP_403_FORBIDDEN
        )

        models.CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).update(applicant_visible=True)

        self.assertEqual(self._get(self.applicant).status_code, status.HTTP_200_OK)

    def test_a_declined_award_is_never_shown_to_the_applicant(self):
        self._decide(outcome=WorkflowStepOutcomes.DECLINED)

        response = self._get(self.applicant)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_panel_member_of_the_call_does_not_see_the_award(self):
        panel_member = structure_factories.UserFactory()
        self.call.add_user(panel_member, CallRole.PANEL_MEMBER)

        self.assertIn(
            self._get(panel_member).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_a_reviewer_of_the_call_does_not_see_the_award(self):
        response = self._get(self.fixture.reviewer_1)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_a_reviewer_does_not_see_the_award_once_released_either(self):
        # Released to the applicant, not to everyone who can open the proposal.
        self._decide()

        response = self._get(self.fixture.reviewer_1)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_the_call_manager_sees_the_award(self):
        self.assertEqual(self._get(self.call_manager).status_code, status.HTTP_200_OK)

    def test_a_call_organiser_sees_the_award(self):
        organiser = self.fixture.call_organizer_user

        self.assertEqual(self._get(organiser).status_code, status.HTTP_200_OK)

    def test_support_sees_the_award(self):
        support = structure_factories.UserFactory(is_support=True)

        self.assertEqual(self._get(support).status_code, status.HTTP_200_OK)

    def test_the_manager_of_a_requested_offering_sees_the_award(self):
        # The technical assessor of the offering the request named.
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        self.assertEqual(self._get(manager).status_code, status.HTTP_200_OK)

    def test_the_manager_of_an_awarded_offering_sees_the_proposal_and_award(self):
        # Nothing was requested on this offering: the call manager moved the
        # item there, so its manager is the one who will provision it.
        awarded = self._awarded()
        awarded.requested_offering = self.other_requested_offering
        awarded.plan = self.other_plan
        awarded.save()
        manager = structure_factories.UserFactory()
        self.other_offering.add_user(manager, OfferingRole.MANAGER)

        self.client.force_authenticate(manager)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_an_unrelated_offering_manager_does_not_see_the_proposal(self):
        unrelated_offering, _ = _limit_offering()
        manager = structure_factories.UserFactory()
        unrelated_offering.add_user(manager, OfferingRole.MANAGER)

        self.assertEqual(self._get(manager).status_code, status.HTTP_404_NOT_FOUND)


class HeldDecisionAwardTest(AwardedResourcesBase):
    """A call that publishes its decisions with the round holds them back."""

    def setUp(self):
        super().setUp()
        CallRole.MANAGER.add_permission(PermissionEnum.UPDATE_CALL)
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save(update_fields=["publish_results"])
        self.round = self.proposal.round
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])
        self._start_allocation()
        self.awarded = self._awarded()
        self.awarded.limits = {"cpu": 40}
        self.awarded.save()
        self._decide()
        self.assertTrue(self.proposal.decision_held)

    def _get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self._list_url())

    def _patch(self, limits):
        self.client.force_authenticate(self.call_manager)
        return self.client.patch(
            self._detail_url(self.awarded), {"limits": limits}, format="json"
        )

    def _release(self):
        with transaction.atomic():
            proposal = models.Proposal.objects.select_for_update().get(
                pk=self.proposal.pk
            )
            workflow_service.release_held_decision(proposal)
        self.proposal.refresh_from_db()

    def test_a_held_award_is_not_editable(self):
        response = self._patch({"cpu": 30})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.limits, {"cpu": 40})

    def test_reopening_the_held_decision_makes_the_award_editable(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="reopen_decision"),
            {"reason": "The board changed the amounts."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        response = self._patch({"cpu": 30})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.limits, {"cpu": 30})

    def test_the_call_manager_sees_the_held_award(self):
        self.assertEqual(self._get(self.call_manager).status_code, status.HTTP_200_OK)

    def test_the_applicant_does_not_see_a_held_award(self):
        # Even where the call shows the decision step to applicants: until
        # the round publishes, the decision is still being made to them.
        models.CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).update(transition_mode="manual", applicant_visible=True)

        self.assertEqual(
            self._get(self.applicant).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_releasing_a_held_approval_provisions_the_award(self):
        self._release()

        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        resource = marketplace_models.Resource.objects.get(
            project=self.proposal.project
        )
        self.assertEqual(resource.limits, {"cpu": 40})
        self.assertEqual(self._get(self.applicant).status_code, status.HTTP_200_OK)


class BackfillMigrationTest(AwardedResourcesBase):
    def _backfill(self):
        backfill_migration.prefill_open_allocations(django_apps, None)
        self.proposal.refresh_from_db()

    def _at(self, step, project=None):
        self.proposal.workflow_step = step
        self.proposal.project = project
        self.proposal.save()

    def test_a_proposal_at_the_allocation_decision_is_prefilled(self):
        self._at("allocation_decision")

        self._backfill()

        awarded = self._awarded()
        self.assertEqual(awarded.requested_resource, self.requested_resource)
        self.assertIsNone(awarded.plan)
        self.assertEqual(awarded.effective_plan, self.plan)
        self.assertEqual(awarded.limits, {"cpu": 100})
        self.assertIsNotNone(self.proposal.awarded_resources_prefilled_at)

    def test_a_proposal_waiting_for_the_award_response_is_prefilled(self):
        self._at("award_response")

        self._backfill()

        self.assertEqual(self.proposal.awarded_resources.count(), 1)

    def test_an_allocated_proposal_is_left_alone(self):
        self._at("award_response", project=self.fixture.proposal_project)

        self._backfill()

        self.assertFalse(self.proposal.awarded_resources.exists())

    def test_a_proposal_before_the_decision_is_left_alone(self):
        self._backfill()

        self.assertFalse(self.proposal.awarded_resources.exists())
