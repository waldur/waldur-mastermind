"""A call's resource budget: the component rules bounding a proposal's
request, and the pools -- call-wide and per round -- its allocation decisions
award from."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import budgets, call_transfer, models, utils
from waldur_mastermind.proposal.enums import (
    DEFAULT_CALL_STEPS,
    BudgetEnforcement,
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
    RoundLifecycleStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_automatic_decisions import (
    AutomaticDecisionBase,
)

NOTIFY = "waldur_mastermind.proposal.tasks.notify_proposal_decision"


class BudgetTestBase(test.APITestCase):
    """An active call offering an offering whose plan prices a limit
    component ``cpu``, one ended round, and a call manager."""

    def setUp(self):
        for permission in (
            PermissionEnum.CLOSE_ROUNDS,
            PermissionEnum.LIST_CALLS,
            PermissionEnum.LIST_PROPOSALS,
            PermissionEnum.LIST_ROUNDS,
            PermissionEnum.UPDATE_CALL,
        ):
            CallRole.MANAGER.add_permission(permission)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now - timedelta(minutes=1),
        )
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])
        self.offering = marketplace_factories.OfferingFactory()
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
            measured_unit="core-hours",
            limit_period=LimitPeriods.TOTAL,
            min_value=1,
            max_value=1000,
        )
        self.requested_offering = factories.RequestedOfferingFactory(
            call=self.call, offering=self.offering
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.requested_offering.plan, component=self.component
        )

    def rule(self, component_type="cpu", requested_offering=None, **kwargs):
        """The component rule of the offering, created or updated."""
        rule, created = models.CallOfferingComponent.objects.get_or_create(
            requested_offering=requested_offering or self.requested_offering,
            component_type=component_type,
            defaults=kwargs,
        )
        if not created and kwargs:
            for key, value in kwargs.items():
                setattr(rule, key, value)
            rule.save()
        return rule

    def budget(
        self, offered_total, call_round=None, requested_offering=None, **rule_kwargs
    ):
        """A pool of the offering's ``cpu``: call-wide, or ``call_round``'s."""
        return models.CallResourcePool.objects.create(
            offering_component=self.rule(
                requested_offering=requested_offering, **rule_kwargs
            ),
            round=call_round,
            offered_total=offered_total,
        )

    def make_decidable(
        self, amount, call_round=None, submitted_at=None, requested_offering=None
    ):
        """A proposal at its allocation decision whose award grants ``amount``."""
        proposal = factories.ProposalFactory(
            round=call_round or self.round,
            state=ProposalStates.IN_REVIEW,
            project=None,
            submitted_at=submitted_at or timezone.now(),
            awarded_resources_prefilled_at=timezone.now(),
        )
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        for step_def in DEFAULT_CALL_STEPS:
            factories.ensure_default_step(self.call, step_def.key)
            if step_def.key == "administrative_check":
                step_status = WorkflowStepInstanceStatuses.COMPLETED
            elif step_def.key == "allocation_decision":
                step_status = WorkflowStepInstanceStatuses.ACTIVE
            else:
                step_status = WorkflowStepInstanceStatuses.SKIPPED
            factories.ProposalWorkflowStepInstanceFactory(
                proposal=proposal,
                step=step_def.key,
                status=step_status,
                started_at=timezone.now()
                if step_status == WorkflowStepInstanceStatuses.ACTIVE
                else None,
            )
        models.AwardedResource.objects.create(
            proposal=proposal,
            requested_offering=requested_offering or self.requested_offering,
            limits={"cpu": amount},
        )
        return proposal

    def decision(self, proposal):
        return proposal.workflow_step_instances.get(step="allocation_decision")

    def decide(self, proposal, outcome="approved", user=None, **extra):
        self.client.force_authenticate(user or self.call_manager)
        url = factories.ProposalFactory.get_url(
            proposal, action="complete_workflow_step"
        )
        with mock.patch(NOTIFY), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url,
                {
                    "step_uuid": self.decision(proposal).uuid.hex,
                    "outcome": outcome,
                    **extra,
                },
            )
        proposal.refresh_from_db()
        return response

    def url(self, action):
        return factories.CallFactory.get_protected_url(self.call, action=action)

    def other_round(self):
        return factories.RoundFactory(
            call=self.call,
            start_time=self.round.start_time - timedelta(days=60),
            cutoff_time=self.round.start_time - timedelta(days=31),
        )


class ComponentRulesTest(BudgetTestBase):
    def create(self, user=None, **data):
        self.client.force_authenticate(user or self.call_manager)
        payload = {
            "requested_offering_uuid": self.requested_offering.uuid.hex,
            "component_type": "cpu",
            **data,
        }
        return self.client.post(self.url("offering_components"), payload, format="json")

    def test_call_manager_sets_lists_and_edits_a_rule(self):
        response = self.create(min_per_proposal=10, max_per_proposal=200)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["max_per_proposal"], 200)
        self.assertEqual(response.data["measured_unit"], "core-hours")
        self.assertEqual(response.data["limit_period"], LimitPeriods.TOTAL)
        self.assertFalse(response.data["is_locked"])

        response = self.client.get(self.url("offering_components"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [row] = response.data

        response = self.client.patch(row["url"], {"default_amount": 50}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rule = models.CallOfferingComponent.objects.get()
        self.assertEqual(rule.default_amount, 50)
        self.assertEqual(rule.max_per_proposal, 200)

    def test_a_fixed_rule_is_its_default_amount(self):
        response = self.create(is_fixed=True, default_amount=100)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rule = models.CallOfferingComponent.objects.get()
        self.assertEqual((rule.min_per_proposal, rule.max_per_proposal), (100, 100))

    def test_invalid_rules_are_refused(self):
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="onboarding",
            billing_type=BillingTypes.ONE_TIME,
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.requested_offering.plan,
            component=self.offering.components.get(type="onboarding"),
        )
        # A limit component the plan does not price.
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="gpu", billing_type=BillingTypes.LIMIT
        )
        cases = [
            {"min_per_proposal": 300, "max_per_proposal": 200},
            {"is_fixed": True},
            {"is_fixed": True, "default_amount": 50, "max_per_proposal": 60},
            {"min_per_proposal": 10, "max_per_proposal": 20, "default_amount": 30},
            {"component_type": "ram"},
            {"component_type": "onboarding"},
            {"component_type": "gpu"},
            {"max_per_proposal": 5000},
            {"min_per_proposal": 0.5},
        ]
        for data in cases:
            response = self.create(**data)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, (data, response.data)
            )
        self.assertFalse(models.CallOfferingComponent.objects.exists())

    def test_one_rule_per_component(self):
        self.assertEqual(self.create().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.create().status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_offering_awaiting_its_provider_takes_rules(self):
        self.requested_offering.state = "requested"
        self.requested_offering.save()
        self.assertEqual(self.create().status_code, status.HTTP_201_CREATED)

    def test_rules_are_fixed_once_a_proposal_requesting_the_offering_is_submitted(
        self,
    ):
        rule = self.rule(max_per_proposal=200)
        proposal = factories.ProposalFactory(
            round=self.round,
            state=ProposalStates.SUBMITTED,
            submitted_at=timezone.now(),
        )
        factories.RequestedResourceFactory(
            proposal=proposal, requested_offering=self.requested_offering
        )
        self.client.force_authenticate(self.call_manager)
        detail = (
            factories.CallFactory.get_protected_url(
                self.call, action="offering_components"
            )
            + f"{rule.uuid.hex}/"
        )

        response = self.client.patch(detail, {"max_per_proposal": 300}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.delete(detail).status_code, 409)
        self.assertEqual(
            self.create(component_type="cpu").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        response = self.client.get(self.url("offering_components"))
        self.assertTrue(response.data[0]["is_locked"])

    def test_a_rule_with_pools_is_not_deleted(self):
        pool = self.budget(offered_total=100)
        self.client.force_authenticate(self.call_manager)
        detail = (
            self.url("offering_components") + f"{pool.offering_component.uuid.hex}/"
        )
        self.assertEqual(self.client.delete(detail).status_code, 409)
        pool.delete()
        self.assertEqual(self.client.delete(detail).status_code, 204)

    def test_template_limits_must_lie_inside_the_bounds(self):
        models.CallResourceTemplate.objects.create(
            call=self.call,
            requested_offering=self.requested_offering,
            name="Big",
            limits={"cpu": 500},
        )
        response = self.create(max_per_proposal=200)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Big", str(response.data))

        self.rule(max_per_proposal=200)
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self.url("resource_templates"),
            {
                "name": "Too big",
                "requested_offering": factories.RequestedOfferingFactory.get_url(
                    self.call, self.requested_offering
                ),
                "limits": {"cpu": 300},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)

    def test_the_offering_lists_its_rules_and_what_they_can_be_set_for(self):
        self.rule(max_per_proposal=200)
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self.url("offerings"))
        [row] = response.data
        self.assertEqual(
            [rule["component_type"] for rule in row["component_rules"]], ["cpu"]
        )
        [component] = row["requestable_components"]
        self.assertEqual(component["type"], "cpu")
        self.assertEqual(component["measured_unit"], "core-hours")
        self.assertFalse(row["rules_locked"])

    def test_limits_on_the_offering_request_are_refused(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.url("offerings"),
            {
                "offering": marketplace_factories.OfferingFactory.get_public_url(
                    self.offering
                ),
                "attributes": {"limits": {"cpu": 500000}},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes", response.data)

    def test_a_plan_that_does_not_price_a_ruled_component_is_refused(self):
        self.rule(max_per_proposal=200)
        self.requested_offering.state = "requested"
        self.requested_offering.save()
        other_plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.client.force_authenticate(self.call_manager)
        response = self.client.patch(
            factories.RequestedOfferingFactory.get_url(
                self.call, self.requested_offering
            ),
            {"plan": marketplace_factories.PlanFactory.get_url(other_plan)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cpu", str(response.data["plan"]))

    def test_public_call_carries_the_bounds_without_the_pools(self):
        self.budget(offered_total=900, max_per_proposal=200)
        response = self.client.get(factories.CallFactory.get_public_url(self.call))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [row] = response.data["component_rules"]
        self.assertEqual(row["max_per_proposal"], 200)
        self.assertEqual(row["measured_unit"], "core-hours")
        self.assertNotIn("offered_total", row)
        self.assertNotIn("offered_total", str(response.data))


class ResourcePoolsTest(BudgetTestBase):
    def create(self, user=None, **data):
        self.client.force_authenticate(user or self.call_manager)
        payload = {
            "offering_component_uuid": self.rule().uuid.hex,
            "offered_total": 100,
            **data,
        }
        return self.client.post(self.url("resource_pools"), payload, format="json")

    def test_call_wide_and_round_pools_coexist(self):
        response = self.create(offered_total=1000)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["round_uuid"])
        response = self.create(offered_total=400, round_uuid=self.round.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["round_name"], self.round.name)

        # One of each per component.
        self.assertEqual(self.create().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.create(round_uuid=self.round.uuid.hex).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(models.CallResourcePool.objects.count(), 2)

    def test_a_pool_on_a_component_without_a_rule_adds_one_bounding_nothing(self):
        self.client.force_authenticate(self.call_manager)
        payload = {
            "requested_offering": self.requested_offering.uuid.hex,
            "component": "cpu",
            "offered_total": 500,
        }
        response = self.client.post(self.url("resource_pools"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rule = models.CallOfferingComponent.objects.get()
        self.assertEqual(
            (rule.min_per_proposal, rule.max_per_proposal, rule.is_fixed),
            (None, None, False),
        )
        self.assertEqual(str(response.data["offering_component_uuid"]), rule.uuid.hex)

        # The rule is reused for the next pool of the component.
        response = self.client.post(
            self.url("resource_pools"),
            {**payload, "round_uuid": self.round.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.CallOfferingComponent.objects.count(), 1)

        for bad in ({**payload, "component": "ram"}, {"offered_total": 5}):
            response = self.client.post(self.url("resource_pools"), bad, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_locked_offering_still_takes_a_pool(self):
        proposal = factories.ProposalFactory(
            round=self.round,
            state=ProposalStates.SUBMITTED,
            submitted_at=timezone.now(),
        )
        factories.RequestedResourceFactory(
            proposal=proposal, requested_offering=self.requested_offering
        )
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self.url("resource_pools"),
            {
                "requested_offering": self.requested_offering.uuid.hex,
                "component": "cpu",
                "offered_total": 500,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_only_the_total_of_a_pool_changes(self):
        response = self.create()
        other = self.other_round()
        response = self.client.patch(
            response.data["url"],
            {"offered_total": 800, "round_uuid": other.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        pool = models.CallResourcePool.objects.get()
        self.assertEqual(pool.offered_total, 800)
        self.assertIsNone(pool.round)

    def test_rounds_and_rules_of_another_call_are_refused(self):
        foreign_round = factories.RoundFactory()
        response = self.create(round_uuid=foreign_round.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        foreign_rule = models.CallOfferingComponent.objects.create(
            requested_offering=factories.RequestedOfferingFactory(),
            component_type="cpu",
        )
        response = self.create(offering_component_uuid=foreign_rule.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_the_call_team_staff_and_support_read_pools(self):
        self.budget(offered_total=100)
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        self.assertIn(
            self.client.get(self.url("resource_pools")).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertIn(
            self.client.get(self.url("resource_pool_usage")).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.client.force_authenticate(self.support)
        self.assertEqual(
            self.client.get(self.url("resource_pool_usage")).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.create(user=self.support).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_round_with_pools_cannot_be_deleted(self):
        other = self.other_round()
        self.budget(offered_total=100, call_round=other)
        self.client.force_authenticate(self.call_manager)
        response = self.client.delete(factories.RoundFactory.get_url(self.call, other))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(models.Round.objects.filter(pk=other.pk).exists())


class OfferingDeleteTest(BudgetTestBase):
    def delete(self):
        self.client.force_authenticate(self.call_manager)
        return self.client.delete(
            factories.RequestedOfferingFactory.get_url(
                self.call, self.requested_offering
            )
        )

    def test_an_offering_with_rules_pools_or_templates_is_kept(self):
        self.budget(offered_total=100)
        models.CallResourceTemplate.objects.create(
            call=self.call, requested_offering=self.requested_offering, name="Small"
        )
        response = self.delete()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        detail = str(response.data)
        self.assertIn("cpu", detail)
        self.assertIn("resource pools (1)", detail)
        self.assertIn("Small", detail)
        self.assertTrue(models.CallResourcePool.objects.exists())

    def test_an_offering_with_nothing_set_on_it_is_deleted(self):
        self.assertEqual(self.delete().status_code, status.HTTP_204_NO_CONTENT)


class RequestBoundsTest(BudgetTestBase):
    def setUp(self):
        super().setUp()
        self.proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)

    def request(self, limits):
        self.client.force_authenticate(self.proposal.created_by)
        return self.client.post(
            factories.RequestedResourceFactory.get_list_url(self.proposal),
            {
                "requested_offering_uuid": self.requested_offering.uuid.hex,
                "limits": limits,
            },
            format="json",
        )

    def test_a_request_outside_the_rule_is_refused(self):
        self.rule(min_per_proposal=10, max_per_proposal=200)
        response = self.request({"cpu": 500})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("200", str(response.data["limits"]))
        self.assertEqual(
            self.request({"cpu": 100}).status_code, status.HTTP_201_CREATED
        )

    def test_the_rule_bounds_the_request_whatever_the_round(self):
        self.rule(max_per_proposal=200)
        self.proposal.round = self.other_round()
        self.proposal.save()
        self.assertEqual(
            self.request({"cpu": 500}).status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_a_pool_bounds_no_request(self):
        self.budget(offered_total=10)
        self.assertEqual(
            self.request({"cpu": 500}).status_code, status.HTTP_201_CREATED
        )

    def test_a_fixed_amount_is_set_whatever_was_sent(self):
        self.rule(is_fixed=True, default_amount=200)
        response = self.request({"cpu": 50})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            models.RequestedResource.objects.get(proposal=self.proposal).limits,
            {"cpu": 200},
        )

    def test_without_a_rule_nothing_changes(self):
        self.assertEqual(
            self.request({"cpu": 999}).status_code, status.HTTP_201_CREATED
        )

    def test_the_total_request_is_checked_on_submit(self):
        for _ in range(2):
            factories.RequestedResourceFactory(
                proposal=self.proposal,
                requested_offering=self.requested_offering,
                limits={"cpu": 150},
            )
        self.rule(max_per_proposal=200)
        self.assertEqual(
            budgets.request_out_of_bounds(self.proposal),
            [
                f"{self.offering.name}: cpu must be between 0 and 200 per "
                "proposal, not 300."
            ],
        )


class RequestOrderableTest(BudgetTestBase):
    """A request is held to what the order allocation places from it will
    need: limits only on the plan's limit components, inside their bounds."""

    def setUp(self):
        super().setUp()
        self.proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        self.fixed = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            billing_type=BillingTypes.FIXED,
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.requested_offering.plan, component=self.fixed
        )

    def request(self, limits):
        self.client.force_authenticate(self.proposal.created_by)
        return self.client.post(
            factories.RequestedResourceFactory.get_list_url(self.proposal),
            {
                "requested_offering_uuid": self.requested_offering.uuid.hex,
                "limits": limits,
            },
            format="json",
        )

    def test_a_limit_on_a_fixed_component_is_refused(self):
        response = self.request({"storage": 5})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("storage", str(response.data["limits"]))
        self.assertFalse(self.proposal.requestedresource_set.exists())

    def test_a_limit_on_an_unknown_component_is_refused(self):
        response = self.request({"gpu": 5})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("gpu", str(response.data["limits"]))

    def test_a_limit_outside_the_components_bounds_is_refused(self):
        response = self.request({"cpu": 5000})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("1000", str(response.data["limits"]))

    def test_a_limit_the_order_takes_is_kept(self):
        response = self.request({"cpu": 500})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_an_update_is_held_to_the_components_too(self):
        requested = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"cpu": 10},
        )
        self.client.force_authenticate(self.proposal.created_by)
        response = self.client.patch(
            factories.RequestedResourceFactory.get_url(self.proposal, requested),
            {"limits": {"storage": 5}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        requested.refresh_from_db()
        self.assertEqual(requested.limits, {"cpu": 10})

    def test_a_draft_saved_before_the_check_cannot_be_submitted(self):
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"storage": 5},
        )
        [message] = utils.unorderable_requests(self.proposal)
        self.assertIn(self.offering.name, message)
        self.assertIn("storage", message)

    def test_a_limit_stored_without_a_number_is_refused_not_an_error(self):
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"cpu": None},
        )
        [message] = utils.unorderable_requests(self.proposal)
        self.assertIn("number", message)

    def test_a_request_on_an_offering_no_longer_accepted_is_left_out(self):
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"storage": 5},
        )
        self.requested_offering.state = RequestedOfferingStates.CANCELED
        self.requested_offering.save()
        self.assertEqual(utils.unorderable_requests(self.proposal), [])

    def test_limits_sent_back_unchanged_do_not_block_another_edit(self):
        requested = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"storage": 5},
        )
        self.client.force_authenticate(self.proposal.created_by)
        response = self.client.patch(
            factories.RequestedResourceFactory.get_url(self.proposal, requested),
            {"limits": {"storage": 5}, "description": "Changed"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_template_with_limits_the_offering_cannot_order_is_refused(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self.url("resource_templates"),
            {
                "name": "Fixed storage",
                "requested_offering": factories.RequestedOfferingFactory.get_url(
                    self.call, self.requested_offering
                ),
                "limits": {"storage": 5},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("storage", str(response.data["limits"]))


class DecisionEnforcementTest(BudgetTestBase):
    def test_awarded_amounts_count_not_requested_ones(self):
        pool = self.budget(offered_total=100)
        first = self.make_decidable(60)
        factories.RequestedResourceFactory(
            proposal=first,
            requested_offering=self.requested_offering,
            limits={"cpu": 999},
        )
        self.assertEqual(self.decide(first).status_code, status.HTTP_200_OK)
        self.assertEqual(budgets.awarded_so_far(pool), 60)

    def test_soft_mode_needs_an_override_with_a_reason(self):
        self.budget(offered_total=100)
        self.decide(self.make_decidable(60))
        second = self.make_decidable(60)

        response = self.decide(second)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("remaining 40", str(response.data))
        self.assertEqual(second.state, ProposalStates.IN_REVIEW)

        response = self.decide(second, override_budget=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.decide(
            second, override_budget=True, budget_override_reason="Strategic"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(second.state, ProposalStates.ACCEPTED)
        event = Event.objects.get(event_type=EventType.PROPOSAL_BUDGET_OVERRIDDEN)
        self.assertIn("Strategic", event.message)

    def test_hard_mode_refuses_call_managers_and_lets_staff_override(self):
        self.call.budget_enforcement = BudgetEnforcement.HARD
        self.call.save()
        self.budget(offered_total=100)
        self.decide(self.make_decidable(60))
        second = self.make_decidable(60)

        response = self.decide(
            second, override_budget=True, budget_override_reason="Strategic"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only staff", str(response.data))

        response = self.decide(
            second,
            user=self.staff,
            override_budget=True,
            budget_override_reason="Board decision",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_declining_is_never_blocked(self):
        self.budget(offered_total=10)
        proposal = self.make_decidable(60)
        self.assertEqual(
            self.decide(proposal, outcome="declined").status_code, status.HTTP_200_OK
        )
        self.assertEqual(proposal.state, ProposalStates.REJECTED)

    def test_a_rejected_award_releases_its_amount(self):
        pool = self.budget(offered_total=100)
        first = self.make_decidable(60)
        self.decide(first)
        self.assertEqual(budgets.awarded_so_far(pool), 60)
        first.state = ProposalStates.CANCELED
        first.save()
        self.assertEqual(budgets.awarded_so_far(pool), 0)

    def test_a_moved_award_counts_against_its_new_offering(self):
        other_offering = marketplace_factories.OfferingFactory()
        other = factories.RequestedOfferingFactory(
            call=self.call, offering=other_offering
        )
        pool = self.budget(offered_total=100)
        other_pool = self.budget(offered_total=100, requested_offering=other)
        proposal = self.make_decidable(60)
        proposal.awarded_resources.update(requested_offering=other)
        self.decide(proposal)
        self.assertEqual(budgets.awarded_so_far(pool), 0)
        self.assertEqual(budgets.awarded_so_far(other_pool), 60)

    def test_a_pool_of_another_round_does_not_apply(self):
        self.budget(offered_total=0, call_round=self.other_round())
        proposal = self.make_decidable(60)
        self.assertEqual(self.decide(proposal).status_code, status.HTTP_200_OK)

    def test_a_million_per_call_at_most_400k_per_cut_off(self):
        """The call-wide pool and the round's both hold an award."""
        self.call.budget_enforcement = BudgetEnforcement.HARD
        self.call.save()
        earlier = self.other_round()
        self.budget(offered_total=1000)
        self.budget(offered_total=400, call_round=earlier)
        self.budget(offered_total=400, call_round=self.round)

        # The earlier cut-off spends its 400; the call has 600 left.
        self.assertEqual(
            self.decide(self.make_decidable(400, call_round=earlier)).status_code,
            status.HTTP_200_OK,
        )
        # This cut-off's own 400 holds an award of 450, though the call has
        # 600 left.
        response = self.decide(self.make_decidable(450))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(f"({self.round.name})", str(response.data))
        self.assertNotIn("whole call", str(response.data))
        self.assertEqual(
            self.decide(self.make_decidable(300)).status_code, status.HTTP_200_OK
        )

    def test_the_call_wide_pool_holds_what_the_round_pool_would_allow(self):
        self.call.budget_enforcement = BudgetEnforcement.HARD
        self.call.save()
        earlier = self.other_round()
        self.budget(offered_total=500)
        self.budget(offered_total=400, call_round=self.round)
        self.decide(self.make_decidable(300, call_round=earlier))

        response = self.decide(self.make_decidable(250))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("whole call", str(response.data))
        self.assertIn("remaining 200", str(response.data))

    def test_the_round_and_the_pools_are_locked_in_that_order(self):
        pools = [
            self.budget(offered_total=100, call_round=self.round),
            self.budget(offered_total=1000),
        ]
        proposal = self.make_decidable(10)
        with transaction.atomic(), CaptureQueriesContext(connection) as queries:
            locked = budgets.locked_pools(proposal.round)
        self.assertEqual([p.pk for p in locked], sorted(p.pk for p in pools))
        locks = [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"]]
        self.assertEqual(len(locks), 2)
        self.assertIn('FROM "proposal_round"', locks[0])
        self.assertIn('FROM "proposal_callresourcepool"', locks[1])
        self.assertIn('ORDER BY "proposal_callresourcepool"."id" ASC', locks[1])

    def test_the_call_team_sees_where_the_award_stands(self):
        self.budget(offered_total=100)
        self.decide(self.make_decidable(60))
        proposal = self.make_decidable(50)
        url = factories.ProposalFactory.get_url(proposal, action="budget")

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [entry] = response.data["entries"]
        self.assertEqual(entry["awarded_so_far"], 60)
        self.assertEqual(entry["remaining"], 40)
        self.assertEqual(entry["award"], 50)
        self.assertIsNone(entry["round_name"])
        self.assertFalse(entry["fits"])

        self.client.force_authenticate(proposal.created_by)
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


class PoolUsageTest(BudgetTestBase):
    def test_every_component_a_proposal_requests_has_a_row(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self.url("resource_pool_usage"))
        [row] = response.data
        self.assertIsNone(row["offering_component_uuid"])
        self.assertEqual(
            row["requested_offering_uuid"], self.requested_offering.uuid.hex
        )
        self.assertEqual(row["component_type"], "cpu")
        self.assertTrue(all(cell["pool_uuid"] is None for cell in row["cells"]))

    def test_usage_per_component_and_round(self):
        earlier = self.other_round()
        self.budget(offered_total=1000)
        self.budget(offered_total=100, call_round=self.round)
        proposal = self.make_decidable(60)
        factories.RequestedResourceFactory(
            proposal=proposal,
            requested_offering=self.requested_offering,
            limits={"cpu": 80},
        )
        self.decide(proposal)
        self.decide(self.make_decidable(30, call_round=earlier))

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self.url("resource_pool_usage"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [row] = response.data
        self.assertEqual(row["measured_unit"], "core-hours")
        call_wide, first, second = row["cells"]
        self.assertIsNone(call_wide["round_uuid"])
        self.assertEqual(call_wide["offered_total"], 1000)
        self.assertEqual(call_wide["awarded"], 90)
        self.assertEqual(call_wide["requested"], 80)
        self.assertEqual(call_wide["remaining"], 910)
        # Rounds in the order they start.
        self.assertEqual(first["round_name"], earlier.name)
        self.assertIsNone(first["pool_uuid"])
        self.assertIsNone(first["remaining"])
        self.assertEqual(first["awarded"], 30)
        self.assertEqual(second["offered_total"], 100)
        self.assertEqual(second["awarded"], 60)
        self.assertEqual(second["remaining"], 40)

    def test_a_round_never_has_more_left_than_the_call(self):
        # A round's award also draws on the whole call's pool: what remains
        # of a round is capped by what remains of the call, whether its own
        # pool is larger or the call's has gone to other rounds.
        earlier = self.other_round()
        self.budget(offered_total=100)
        self.budget(offered_total=1000, call_round=self.round)
        self.decide(self.make_decidable(30, call_round=earlier))

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self.url("resource_pool_usage"))
        [row] = response.data
        call_wide, _first, second = row["cells"]
        self.assertEqual(call_wide["remaining"], 70)
        self.assertEqual(second["offered_total"], 1000)
        self.assertEqual(second["remaining"], 70)


class PeriodicAccountingTest(BudgetTestBase):
    """A periodic amount counts once per period the item runs; a total one
    once."""

    def set_period(self, period):
        self.component.limit_period = period
        self.component.save()

    def awarded(self, attributes=None, months=None):
        pool = self.budget(offered_total=10000)
        proposal = self.make_decidable(100)
        proposal.awarded_resources.update(attributes=attributes or {})
        if months:
            self.call.allowed_duration_months = [months]
            self.call.save()
            proposal.awarded_duration_months = months
            proposal.save()
        self.decide(proposal)
        return budgets.awarded_so_far(pool)

    def test_a_total_limit_counts_once(self):
        self.set_period(LimitPeriods.TOTAL)
        self.assertEqual(self.awarded(months=12), 100)

    def test_a_monthly_limit_counts_per_month_of_the_chosen_length(self):
        self.set_period(LimitPeriods.MONTH)
        self.assertEqual(self.awarded(months=12), 1200)

    def test_the_subscription_of_the_item_comes_first(self):
        self.set_period(LimitPeriods.MONTH)
        self.assertEqual(
            self.awarded(attributes={"prepaid_duration_months": 6}, months=12), 600
        )

    def test_a_begun_quarter_counts_whole(self):
        self.set_period(LimitPeriods.QUARTERLY)
        self.assertEqual(self.awarded(months=4), 200)

    def test_the_fixed_duration_in_whole_months(self):
        self.set_period(LimitPeriods.ANNUAL)
        self.call.fixed_duration_in_days = 730
        self.call.save()
        self.assertEqual(self.awarded(), 200)

    def test_with_no_length_known_a_periodic_limit_counts_once(self):
        self.set_period(LimitPeriods.MONTH)
        self.assertEqual(self.awarded(), 100)

    def test_the_award_is_held_to_what_it_draws_over_its_length(self):
        self.set_period(LimitPeriods.MONTH)
        self.call.budget_enforcement = BudgetEnforcement.HARD
        self.call.allowed_duration_months = [12]
        self.call.save()
        self.budget(offered_total=1000)
        proposal = self.make_decidable(100)
        proposal.awarded_duration_months = 12
        proposal.save()
        response = self.decide(proposal)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("award 1200, remaining 1000", str(response.data))

    def test_months_and_periods(self):
        self.assertEqual(budgets.item_months({}, None, 365), 12)
        self.assertEqual(budgets.item_months({}, None, 10), 1)
        self.assertEqual(budgets.item_months({}, 6, 365), 6)
        self.assertEqual(budgets.item_months({"prepaid_duration_months": 3}, 6), 3)
        self.assertIsNone(budgets.item_months({}))
        self.assertEqual(budgets.periods(LimitPeriods.MONTH, 7), 7)
        self.assertEqual(budgets.periods(LimitPeriods.QUARTERLY, 7), 3)
        self.assertEqual(budgets.periods(LimitPeriods.ANNUAL, 13), 2)
        self.assertEqual(budgets.periods(LimitPeriods.TOTAL, 13), 1)
        self.assertEqual(budgets.periods(LimitPeriods.MONTH, None), 1)
        self.assertEqual(
            budgets.counted_items(models.AwardedResource.objects.none()), []
        )
        self.assertEqual(Decimal(budgets.json_number(Decimal("2.50"))), Decimal("2.5"))


class ApplicantTeamDecisionTest(BudgetTestBase):
    def test_a_call_manager_on_the_applicant_team_cannot_decide(self):
        proposal = self.make_decidable(10)
        proposal.add_user(self.call_manager, ProposalRole.MEMBER)
        response = self.decide(proposal)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)


class CopyAndTransferTest(BudgetTestBase):
    def test_duplicating_copies_rules_and_pools_onto_the_copies(self):
        self.budget(offered_total=900, max_per_proposal=200)
        self.budget(offered_total=300, call_round=self.round)
        new_call = utils.duplicate_call(self.call, "Copy", self.call_manager)
        [rule] = models.CallOfferingComponent.objects.filter(
            requested_offering__call=new_call
        )
        self.assertEqual(rule.max_per_proposal, 200)
        pools = {pool.round_id: pool for pool in rule.pools.select_related("round")}
        self.assertEqual(pools[None].offered_total, 900)
        [round_pool] = [pool for key, pool in pools.items() if key is not None]
        self.assertEqual(round_pool.offered_total, 300)
        self.assertEqual(round_pool.round.call, new_call)

        new_call = utils.duplicate_call(
            self.call, "No pools", self.call_manager, {"copy_budgets": False}
        )
        rule = models.CallOfferingComponent.objects.get(
            requested_offering__call=new_call
        )
        self.assertFalse(rule.pools.exists())

    def test_export_and_import_round_trip(self):
        self.budget(offered_total=900, max_per_proposal=200, default_amount=50)
        self.budget(offered_total=300, call_round=self.round)
        document, warnings = call_transfer.export_call(self.call)
        self.assertEqual(warnings, [])
        [entry] = document["requested_offerings"]
        [component] = entry["offering_components"]
        self.assertEqual(
            {p["offered_total"]: p["round"] for p in component["pools"]},
            {"900.00": None, "300.00": 0},
        )
        call, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.call_manager
        )
        [rule] = models.CallOfferingComponent.objects.filter(
            requested_offering__call=call
        )
        self.assertEqual(rule.max_per_proposal, 200)
        self.assertEqual(rule.default_amount, 50)
        totals = {
            pool.round.call_id if pool.round else None: pool.offered_total
            for pool in rule.pools.select_related("round")
        }
        self.assertEqual(totals, {None: 900, call.pk: 300})
        self.assertEqual(call.budget_enforcement, BudgetEnforcement.SOFT)

    def test_without_the_rounds_only_call_wide_pools_travel(self):
        self.budget(offered_total=900)
        self.budget(offered_total=300, call_round=self.round)
        document, warnings = call_transfer.export_call(
            self.call, sections={"rounds": False}
        )
        [component] = document["requested_offerings"][0]["offering_components"]
        self.assertEqual([p["round"] for p in component["pools"]], [None])
        self.assertTrue(any("not exported" in w for w in warnings), warnings)


class AutomaticDecisionBudgetTest(AutomaticDecisionBase):
    """An automatic decision whose award does not fit a pool is left to the
    call managers, never declined."""

    def _exhaust(self, enforcement, call_round=None):
        self.call.budget_enforcement = enforcement
        self.call.save()
        # Both offerings take the amount, so only the pool stands in the way.
        for requested in (self.requested_a, self.requested_b):
            marketplace_factories.PlanComponentFactory(
                plan=requested.plan,
                component=marketplace_factories.OfferingComponentFactory(
                    offering=requested.offering,
                    type="cpu",
                    billing_type=BillingTypes.LIMIT,
                ),
            )
        self.proposal.requestedresource_set.update(limits={"cpu": 50})
        rule = models.CallOfferingComponent.objects.create(
            requested_offering=self.requested_a, component_type="cpu"
        )
        models.CallResourcePool.objects.create(
            offering_component=rule, round=call_round, offered_total=10
        )

    def test_an_exhausted_hard_pool_falls_back_to_the_call_managers(self):
        self._exhaust(BudgetEnforcement.HARD)
        with mock.patch(NOTIFY) as notify:
            self._assess_both()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            self._decision_instance().status, WorkflowStepInstanceStatuses.ACTIVE
        )
        notify.assert_not_called()
        [event] = self._deferred_events()
        self.assertIn("budget", event.message)

    def test_an_exhausted_round_pool_also_falls_back(self):
        self._exhaust(BudgetEnforcement.HARD, call_round=self.fixture.round)
        with mock.patch(NOTIFY):
            self._assess_both()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertTrue(self._deferred_events().exists())

    def test_a_soft_pool_also_waits_for_an_override(self):
        self._exhaust(BudgetEnforcement.SOFT)
        with mock.patch(NOTIFY):
            self._assess_both()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertTrue(self._deferred_events().exists())
