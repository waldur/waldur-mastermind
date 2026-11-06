"""What a provider, the call manager or staff may change under a running
call, and what allocation does when the provider changed something anyway."""

from django.db.models import ProtectedError
from rest_framework import status, test

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.proposal import budgets, models, provider_calls, utils
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
)

from . import factories


@override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
class CallGuardsBase(test.APITestCase):
    """An active call takes in an offering; a proposal in review requests
    100 CPU hours of it."""

    proposal_state = ProposalStates.IN_REVIEW

    def setUp(self):
        for permission in (
            PermissionEnum.PAUSE_OFFERING,
            PermissionEnum.ARCHIVE_OFFERING,
            PermissionEnum.CREATE_OFFERING,
            PermissionEnum.ARCHIVE_OFFERING_PLAN,
        ):
            CustomerRole.OWNER.add_permission(permission)
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.owner = self.fixture.offering_owner
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.plan = self.fixture.plan
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            billing_type=BillingTypes.LIMIT,
            limit_period="",
            max_value=None,
            min_value=None,
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.component, price=1
        )
        self.call = factories.CallFactory(state=CallStates.ACTIVE, name="Regular")
        self.requested = factories.RequestedOfferingFactory(
            call=self.call,
            offering=self.offering,
            plan=self.plan,
            state=RequestedOfferingStates.ACCEPTED,
        )
        self.rule = models.CallOfferingComponent.objects.create(
            requested_offering=self.requested,
            component_type="cpu_hours",
            max_per_proposal=500,
        )
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(
            round=self.round, state=self.proposal_state, project=None
        )
        self.request = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested,
            limits={"cpu_hours": 100},
        )

    def post(self, user, url, data=None):
        self.client.force_authenticate(user)
        return self.client.post(url, data or {}, format="json")

    def offering_action(self, user, action, data=None):
        return self.post(
            user,
            marketplace_factories.OfferingFactory.get_url(self.offering, action),
            data,
        )


class OfferingLifecycleTest(CallGuardsBase):
    def test_the_provider_cannot_withdraw_an_offering_proposals_wait_on(self):
        for action in ("archive", "pause", "draft", "make_unavailable"):
            response = self.offering_action(self.owner, action)
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, action)
            self.assertIn("Regular", response.data["detail"])
            self.offering.refresh_from_db()
            self.assertEqual(self.offering.state, OfferingStates.ACTIVE)

    def test_staff_can(self):
        response = self.offering_action(self.staff, "pause")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_nobody_deletes_an_offering_a_call_depends_on(self):
        self.client.force_authenticate(self.staff)
        response = self.client.delete(
            marketplace_factories.OfferingFactory.get_url(self.offering)
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(pk=self.offering.pk).exists()
        )

    def test_the_impact_is_shown_before_the_change(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            marketplace_factories.OfferingFactory.get_url(self.offering, "dependents"),
            {"change": "offering_withdrawn"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        (dependent,) = response.data
        self.assertTrue(dependent["blocks"])
        self.assertFalse(dependent["blocks_staff"])
        self.assertIn("Regular", dependent["summary"])


class OfferingWithNothingWaitingTest(CallGuardsBase):
    proposal_state = ProposalStates.REJECTED

    def test_the_provider_may_withdraw_it_and_is_told_of_the_call(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            marketplace_factories.OfferingFactory.get_url(self.offering, "dependents"),
            {"change": "offering_withdrawn"},
        )
        (dependent,) = response.data
        self.assertFalse(dependent["blocks"])
        response = self.offering_action(self.owner, "pause")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class PlanGuardTest(CallGuardsBase):
    def test_the_provider_cannot_archive_the_call_plan(self):
        response = self.post(
            self.owner, marketplace_factories.PlanFactory.get_url(self.plan, "archive")
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.archived)

    def test_deleting_the_plan_cannot_take_the_call_offering_with_it(self):
        with self.assertRaises(ProtectedError):
            self.plan.delete()


class ComponentGuardTest(CallGuardsBase):
    def component_action(self, action, data):
        return self.post(
            self.staff,
            marketplace_factories.OfferingFactory.get_url(self.offering, action),
            {"uuid": self.component.uuid.hex, **data},
        )

    def test_a_component_the_call_names_is_not_removed(self):
        response = self.component_action("remove_offering_component", {})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(
            marketplace_models.OfferingComponent.objects.filter(
                pk=self.component.pk
            ).exists()
        )

    def test_a_requested_component_keeps_being_billed_by_its_limit(self):
        response = self.component_action(
            "update_offering_component", {"billing_type": BillingTypes.USAGE}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_its_maximum_stays_above_what_the_call_allows(self):
        response = self.component_action("update_offering_component", {"max_value": 50})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("500", response.data["detail"])
        response = self.component_action(
            "update_offering_component", {"max_value": 1000}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_rename_carries_into_the_call(self):
        response = self.component_action(
            "update_offering_component", {"type": "core_hours"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.rule.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.rule.component_type, "core_hours")
        self.assertEqual(self.request.limits, {"core_hours": 100})

    def test_a_rule_keeps_the_limit_period_it_was_set_up_with(self):
        self.component.limit_period = "month"
        self.component.save()
        rule = models.CallOfferingComponent.objects.create(
            requested_offering=factories.RequestedOfferingFactory(
                call=factories.CallFactory(),
                offering=self.offering,
                plan=self.plan,
                state=RequestedOfferingStates.ACCEPTED,
            ),
            component_type="cpu_hours",
        )
        self.assertEqual(rule.limit_period, "month")


class AllocationRevalidatesTest(CallGuardsBase):
    proposal_state = ProposalStates.ACCEPTED

    def setUp(self):
        super().setUp()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)

    def test_an_item_the_offering_no_longer_takes_is_not_ordered(self):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()
        utils.allocate_proposal(self.proposal)
        (item,) = self.proposal.awarded_resources.all()
        self.assertIsNone(item.resource)
        self.assertIn("archived", item.provision_error)
        self.assertFalse(
            marketplace_models.Order.objects.filter(
                project=self.proposal.project
            ).exists()
        )

    def test_limits_the_offering_no_longer_accepts_are_not_ordered(self):
        self.component.max_value = 10
        self.component.save()
        utils.allocate_proposal(self.proposal)
        (item,) = self.proposal.awarded_resources.all()
        self.assertIsNone(item.resource)
        self.assertIn("limits", item.provision_error)

    def test_the_call_manager_orders_it_again_once_it_is_fixed(self):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()
        utils.allocate_proposal(self.proposal)
        (item,) = self.proposal.awarded_resources.all()
        url = factories.ProposalFactory.get_url(self.proposal, "provision_awarded_item")
        payload = {"awarded_resource_uuid": item.uuid.hex}

        response = self.post(self.manager, url, payload)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response = self.post(self.manager, url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item.refresh_from_db()
        self.assertIsNotNone(item.resource)
        self.assertEqual(item.provision_error, "")

        response = self.post(self.manager, url, payload)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_nobody_else_does(self):
        utils.allocate_proposal(self.proposal)
        (item,) = self.proposal.awarded_resources.all()
        response = self.post(
            self.owner,
            factories.ProposalFactory.get_url(self.proposal, "provision_awarded_item"),
            {"awarded_resource_uuid": item.uuid.hex},
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_an_award_the_provider_refused_leaves_the_pool(self):
        utils.allocate_proposal(self.proposal)
        (item,) = self.proposal.awarded_resources.all()
        self.assertTrue(budgets.awarded_items().filter(pk=item.pk).exists())
        marketplace_models.Order.objects.filter(
            resource=item.resource, type=OrderTypes.CREATE
        ).update(state=OrderStates.REJECTED)
        self.assertFalse(budgets.awarded_items().filter(pk=item.pk).exists())


class StaffingTest(CallGuardsBase):
    def test_a_deactivated_manager_staffs_nothing(self):
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.assertEqual(
            provider_calls.offering_manager_counts([self.offering.pk])[
                self.offering.pk
            ],
            1,
        )
        manager.is_active = False
        manager.save()
        self.assertEqual(
            provider_calls.offering_manager_counts([self.offering.pk])[
                self.offering.pk
            ],
            0,
        )


class RecordsTest(CallGuardsBase):
    def test_a_reviewer_with_a_review_is_not_deleted(self):
        review = factories.ReviewFactory(proposal=self.proposal)
        with self.assertRaises(ProtectedError):
            review.reviewer.delete()

    def test_a_form_with_answers_is_not_deleted(self):
        checklist = checklist_factories.ChecklistFactory()
        question = checklist_factories.QuestionFactory(checklist=checklist)
        checklist_factories.AnswerFactory(question=question)
        self.client.force_authenticate(self.staff)
        response = self.client.delete(
            checklist_factories.ChecklistFactory.get_url(checklist)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_archiving_a_call_with_proposals_in_evaluation_needs_confirming(self):
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        url = factories.CallFactory.get_protected_url(self.call, "archive")
        response = self.post(self.staff, url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["proposals_in_evaluation"], 1)
        response = self.post(self.staff, url, {"confirm": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
