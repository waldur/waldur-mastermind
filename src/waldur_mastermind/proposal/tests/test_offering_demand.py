"""What proposals ask of a provider's offerings: per offering, call, round
and group of proposal states, summed per component, with the call's pools,
and read only by the provider side."""

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import CallStates, ProposalStates
from waldur_mastermind.proposal.tests import factories

URL = "http://testserver" + reverse("proposal-offering-demand-list")


class OfferingDemandBase(test.APITestCase):
    """One active call with two rounds, requesting an offering of provider A
    -- a ``cpu`` limited in total and a ``gpu`` limited per month -- and one
    of provider B."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        now = timezone.now()
        self.round_1 = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=60),
            cutoff_time=now - timedelta(days=30),
        )
        self.round_2 = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=10),
            cutoff_time=now + timedelta(days=30),
        )

        self.provider_a = structure_factories.CustomerFactory()
        self.offering_a = marketplace_factories.OfferingFactory(
            customer=self.provider_a
        )
        self.requested_a = self.requested_offering(
            self.offering_a,
            {"cpu": LimitPeriods.TOTAL, "gpu": LimitPeriods.MONTH},
        )
        self.owner_a = structure_factories.UserFactory()
        self.provider_a.add_user(self.owner_a, CustomerRole.OWNER)
        self.manager_a = structure_factories.UserFactory()
        self.offering_a.add_user(self.manager_a, OfferingRole.MANAGER)

        self.provider_b = structure_factories.CustomerFactory()
        self.offering_b = marketplace_factories.OfferingFactory(
            customer=self.provider_b
        )
        self.requested_b = self.requested_offering(
            self.offering_b, {"cpu": LimitPeriods.TOTAL}
        )
        self.owner_b = structure_factories.UserFactory()
        self.provider_b.add_user(self.owner_b, CustomerRole.OWNER)

    def requested_offering(self, offering, periods):
        requested = factories.RequestedOfferingFactory(
            call=self.call, offering=offering
        )
        for component_type, period in periods.items():
            component = marketplace_factories.OfferingComponentFactory(
                offering=offering,
                type=component_type,
                name=component_type.upper(),
                billing_type=BillingTypes.LIMIT,
                measured_unit="hours",
                limit_period=period,
            )
            marketplace_factories.PlanComponentFactory(
                plan=requested.plan, component=component
            )
        return requested

    def proposal(self, state, call_round=None, requests=(), awards=()):
        """A proposal of ``call_round`` asking ``requests`` -- pairs of call
        offering and limits -- and awarded ``awards``."""
        proposal = factories.ProposalFactory(
            round=call_round or self.round_1, state=state, project=None
        )
        for requested_offering, limits in requests:
            factories.RequestedResourceFactory(
                proposal=proposal,
                requested_offering=requested_offering,
                resource=None,
                limits=limits,
            )
        for requested_offering, limits, *attributes in awards:
            models.AwardedResource.objects.create(
                proposal=proposal,
                requested_offering=requested_offering,
                limits=limits,
                attributes=attributes[0] if attributes else {},
            )
        return proposal

    def rows(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.json()

    def row(self, rows, offering, call_round, group):
        matching = [
            row
            for row in rows
            if row["offering_uuid"] == offering.uuid.hex
            and row["round_uuid"] == call_round.uuid.hex
            and row["state_group"] == group
        ]
        self.assertEqual(len(matching), 1, rows)
        return matching[0]

    @staticmethod
    def component(row, component_type):
        (component,) = [c for c in row["components"] if c["type"] == component_type]
        return component


class OfferingDemandScopeTest(OfferingDemandBase):
    def setUp(self):
        super().setUp()
        self.proposal(
            ProposalStates.SUBMITTED,
            requests=[(self.requested_a, {"cpu": 100}), (self.requested_b, {"cpu": 7})],
        )

    def offerings_seen(self, user):
        return {row["offering_uuid"] for row in self.rows(user)}

    def test_provider_owner_sees_only_its_own_offerings(self):
        self.assertEqual(self.offerings_seen(self.owner_a), {self.offering_a.uuid.hex})
        self.assertEqual(self.offerings_seen(self.owner_b), {self.offering_b.uuid.hex})

    def test_offering_manager_sees_the_offering_it_manages(self):
        self.assertEqual(
            self.offerings_seen(self.manager_a), {self.offering_a.uuid.hex}
        )

    def test_another_provider_asking_for_a_foreign_offering_gets_nothing(self):
        self.assertEqual(
            self.rows(self.owner_b, offering_uuid=self.offering_a.uuid.hex), []
        )
        self.assertEqual(
            self.rows(self.owner_b, provider_uuid=self.provider_a.uuid.hex), []
        )

    def test_staff_and_support_see_every_offering(self):
        both = {self.offering_a.uuid.hex, self.offering_b.uuid.hex}
        self.assertEqual(self.offerings_seen(self.staff), both)
        self.assertEqual(self.offerings_seen(self.support), both)

    def test_call_manager_without_an_offering_role_gets_nothing(self):
        call_manager = structure_factories.UserFactory()
        self.call.add_user(call_manager, CallRole.MANAGER)
        self.assertEqual(self.rows(call_manager), [])

    def test_a_proposal_member_gets_nothing(self):
        self.assertEqual(self.rows(structure_factories.UserFactory()), [])

    def test_anonymous_is_refused(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_drafts_are_left_out(self):
        self.proposal(ProposalStates.DRAFT, requests=[(self.requested_a, {"cpu": 9})])
        rows = self.rows(self.owner_a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.component(rows[0], "cpu")["requested"], 100)


class OfferingDemandSumsTest(OfferingDemandBase):
    def setUp(self):
        super().setUp()
        a = self.requested_a
        self.proposal(ProposalStates.SUBMITTED, requests=[(a, {"cpu": 100, "gpu": 10})])
        self.proposal(ProposalStates.IN_REVIEW, requests=[(a, {"cpu": 50, "gpu": 5})])
        self.proposal(
            ProposalStates.IN_REVIEW,
            call_round=self.round_2,
            requests=[(a, {"cpu": 30})],
        )
        self.proposal(
            ProposalStates.ACCEPTED,
            requests=[(a, {"cpu": 200, "gpu": 20})],
            awards=[(a, {"cpu": 150, "gpu": 20})],
        )
        self.proposal(ProposalStates.REJECTED, requests=[(a, {"cpu": 999})])
        self.proposal(ProposalStates.CANCELED, requests=[(a, {"cpu": 999})])

    def test_pending_sums_per_round(self):
        rows = self.rows(self.owner_a)
        pending = self.row(rows, self.offering_a, self.round_1, "pending")
        self.assertEqual(pending["proposals_count"], 2)
        self.assertEqual(self.component(pending, "cpu")["requested"], 150)
        self.assertIsNone(self.component(pending, "cpu")["awarded"])
        later = self.row(rows, self.offering_a, self.round_2, "pending")
        self.assertEqual(later["proposals_count"], 1)
        self.assertEqual(self.component(later, "cpu")["requested"], 30)
        self.assertEqual(self.component(later, "gpu")["requested"], 0)

    def test_periodic_amounts_are_summed_per_period_and_say_the_period(self):
        pending = self.row(
            self.rows(self.owner_a), self.offering_a, self.round_1, "pending"
        )
        gpu = self.component(pending, "gpu")
        self.assertEqual(gpu["requested"], 15)
        self.assertEqual(gpu["limit_period"], LimitPeriods.MONTH)
        self.assertEqual(gpu["measured_unit"], "hours")
        self.assertEqual(gpu["name"], "GPU")
        self.assertEqual(
            self.component(pending, "cpu")["limit_period"], LimitPeriods.TOTAL
        )

    def test_accepted_has_requested_and_awarded(self):
        accepted = self.row(
            self.rows(self.owner_a), self.offering_a, self.round_1, "accepted"
        )
        self.assertEqual(accepted["proposals_count"], 1)
        cpu = self.component(accepted, "cpu")
        self.assertEqual(cpu["requested"], 200)
        self.assertEqual(cpu["awarded"], 150)

    def test_closed_only_counts(self):
        closed = self.row(
            self.rows(self.owner_a), self.offering_a, self.round_1, "closed"
        )
        self.assertEqual(closed["proposals_count"], 2)
        self.assertEqual(closed["components"], [])

    def test_rows_say_where_the_call_and_round_stand(self):
        row = self.row(
            self.rows(self.owner_a), self.offering_a, self.round_2, "pending"
        )
        self.assertEqual(row["call_uuid"], self.call.uuid.hex)
        self.assertEqual(row["call_name"], self.call.name)
        self.assertEqual(row["call_state"], CallStates.ACTIVE)
        self.assertEqual(row["round_name"], self.round_2.name)
        self.assertEqual(row["round_status"], "open")
        self.assertIsNotNone(row["round_cutoff_time"])
        self.assertEqual(row["provider_uuid"], self.provider_a.uuid.hex)
        self.assertEqual(row["requested_offering_uuid"], self.requested_a.uuid.hex)

    def test_rows_are_ordered_by_round_then_group(self):
        rows = self.rows(self.owner_a)
        self.assertEqual(
            [(row["round_uuid"], row["state_group"]) for row in rows],
            [
                (self.round_1.uuid.hex, "pending"),
                (self.round_1.uuid.hex, "accepted"),
                (self.round_1.uuid.hex, "closed"),
                (self.round_2.uuid.hex, "pending"),
            ],
        )

    def test_filters(self):
        self.assertEqual(
            {
                row["state_group"]
                for row in self.rows(self.owner_a, state_group="accepted")
            },
            {"accepted"},
        )
        self.assertEqual(
            {
                row["round_uuid"]
                for row in self.rows(self.owner_a, round_uuid=self.round_2.uuid.hex)
            },
            {self.round_2.uuid.hex},
        )
        other_call = factories.CallFactory()
        self.assertEqual(self.rows(self.owner_a, call_uuid=other_call.uuid.hex), [])
        self.assertTrue(self.rows(self.owner_a, call_uuid=self.call.uuid.hex))
        self.assertTrue(self.rows(self.staff, provider_uuid=self.provider_a.uuid.hex))

    def test_invalid_filters_are_refused(self):
        self.client.force_authenticate(self.owner_a)
        response = self.client.get(URL, {"call_uuid": "nope"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.get(URL, {"state_group": "draft"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_award_moved_here_counts_on_this_offering(self):
        self.proposal(
            ProposalStates.ACCEPTED,
            call_round=self.round_2,
            requests=[(self.requested_b, {"cpu": 40})],
            awards=[(self.requested_a, {"cpu": 40})],
        )
        accepted = self.row(
            self.rows(self.owner_a), self.offering_a, self.round_2, "accepted"
        )
        self.assertEqual(accepted["proposals_count"], 1)
        self.assertEqual(self.component(accepted, "cpu")["requested"], 0)
        self.assertEqual(self.component(accepted, "cpu")["awarded"], 40)


class OfferingDemandPoolsTest(OfferingDemandBase):
    def pool(self, component_type, offered_total, call_round=None):
        rule, _ = models.CallOfferingComponent.objects.get_or_create(
            requested_offering=self.requested_a, component_type=component_type
        )
        return models.CallResourcePool.objects.create(
            offering_component=rule, round=call_round, offered_total=offered_total
        )

    def test_pool_figures_call_wide_and_per_round(self):
        self.pool("cpu", 1000)
        self.pool("cpu", 300, self.round_1)
        a = self.requested_a
        self.proposal(
            ProposalStates.ACCEPTED,
            requests=[(a, {"cpu": 100})],
            awards=[(a, {"cpu": 80})],
        )
        self.proposal(
            ProposalStates.ACCEPTED,
            call_round=self.round_2,
            requests=[(a, {"cpu": 50})],
            awards=[(a, {"cpu": 50})],
        )
        rows = self.rows(self.owner_a)
        first = self.component(
            self.row(rows, self.offering_a, self.round_1, "accepted"), "cpu"
        )
        self.assertEqual(
            first["call_pool"], {"offered": 1000, "awarded": 130, "remaining": 870}
        )
        self.assertEqual(
            first["round_pool"], {"offered": 300, "awarded": 80, "remaining": 220}
        )
        second = self.component(
            self.row(rows, self.offering_a, self.round_2, "accepted"), "cpu"
        )
        self.assertEqual(second["call_pool"]["awarded"], 130)
        self.assertIsNone(second["round_pool"])
        self.assertIsNone(
            self.component(
                self.row(rows, self.offering_a, self.round_1, "accepted"), "gpu"
            )["call_pool"]
        )

    def test_a_periodic_pool_counts_over_the_award_length(self):
        self.pool("gpu", 500)
        a = self.requested_a
        self.proposal(
            ProposalStates.ACCEPTED,
            requests=[(a, {"gpu": 20})],
            awards=[(a, {"gpu": 20}, {"prepaid_duration_months": 3})],
        )
        accepted = self.row(
            self.rows(self.owner_a), self.offering_a, self.round_1, "accepted"
        )
        gpu = self.component(accepted, "gpu")
        # 20 hours a month, as stored, awarded for three months of the pool.
        self.assertEqual(gpu["awarded"], 20)
        self.assertEqual(
            gpu["call_pool"], {"offered": 500, "awarded": 60, "remaining": 440}
        )

    def test_the_round_filter_leaves_the_call_wide_pool_whole(self):
        self.pool("cpu", 1000)
        a = self.requested_a
        self.proposal(
            ProposalStates.ACCEPTED,
            requests=[(a, {"cpu": 70})],
            awards=[(a, {"cpu": 70})],
        )
        self.proposal(
            ProposalStates.IN_REVIEW,
            call_round=self.round_2,
            requests=[(a, {"cpu": 10})],
        )
        (row,) = self.rows(self.owner_a, round_uuid=self.round_2.uuid.hex)
        self.assertEqual(self.component(row, "cpu")["call_pool"]["awarded"], 70)


class OfferingDemandQueryCountTest(OfferingDemandBase):
    def add_proposals(self, count):
        a, b = self.requested_a, self.requested_b
        for index in range(count):
            self.proposal(
                [
                    ProposalStates.SUBMITTED,
                    ProposalStates.ACCEPTED,
                    ProposalStates.REJECTED,
                ][index % 3],
                call_round=[self.round_1, self.round_2][index % 2],
                requests=[(a, {"cpu": 10, "gpu": 1}), (b, {"cpu": 5})],
                awards=[(a, {"cpu": 8})],
            )

    def count_queries(self):
        self.client.force_authenticate(self.owner_a)
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(context.captured_queries), response.json()

    def test_queries_do_not_grow_with_proposals(self):
        rule, _ = models.CallOfferingComponent.objects.get_or_create(
            requested_offering=self.requested_a, component_type="cpu"
        )
        models.CallResourcePool.objects.create(
            offering_component=rule, offered_total=1000
        )
        self.add_proposals(3)
        few, rows = self.count_queries()
        self.assertTrue(rows)
        self.add_proposals(12)
        many, rows = self.count_queries()
        self.assertEqual(sum(row["proposals_count"] for row in rows), 15)
        self.assertEqual(few, many)
