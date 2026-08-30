"""The subscription period an applicant requests must survive allocation.

The resource request names a length (``attributes.prepaid_duration_months``),
with ``attributes.end_date`` the older form of the same answer; the prepaid
multiplier in ``Plan.get_estimate`` and in the invoice item builder both read
``Resource.end_date``. Allocation used to drop the value in between, so a
six-month grant was priced and invoiced as one month.
"""

import datetime
from decimal import Decimal
from unittest import mock

from dateutil.relativedelta import relativedelta
from django.utils import timezone
from rest_framework import test

from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models, utils
from waldur_mastermind.proposal.tests import factories, fixtures

CPU_PRICE = Decimal("2.50")
CPU_HOURS = 1000
MONTHS = 6


class PrepaidFixture:
    """An offering sold by the month, and a proposal asking for it."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.project = None
        self.proposal.save()

        self.offering = self.fixture.offering
        self.offering.components.all().delete()
        self.cpu = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )
        self.plan = marketplace_factories.PlanFactory(
            offering=self.offering, unit="month"
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.cpu, price=CPU_PRICE, amount=0
        )
        self.requested_offering = factories.RequestedOfferingFactory(
            call=self.fixture.call, offering=self.offering, plan=self.plan
        )

    def _request(self, end_date, **attributes):
        if end_date:
            attributes["end_date"] = end_date
        return factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            resource=None,
            limits={"cpu_hours": CPU_HOURS},
            attributes=attributes,
        )

    def _allocate(self, requested_resource):
        utils.allocate_proposal(self.proposal)
        requested_resource.refresh_from_db()
        return requested_resource.resource

    def _age(self, requested_resource, days):
        """Push the row's creation date back, as a draft revisited later is."""
        requested_resource.created = requested_resource.created - datetime.timedelta(
            days=days
        )
        requested_resource.save(update_fields=["created"])
        return requested_resource


class PrepaidDurationTest(PrepaidFixture, test.APITestCase):
    def test_the_requested_period_reaches_the_resource(self):
        today = datetime.date.today()
        requested_resource = self._request(
            (today + datetime.timedelta(days=180)).isoformat()
        )

        resource = self._allocate(requested_resource)

        self.assertIsNotNone(resource.end_date)
        # The length is preserved rather than the absolute date, so the grant
        # runs for the period that was requested and priced.
        self.assertEqual(resource.end_date.month, (today.month + MONTHS - 1) % 12 + 1)

    def test_a_prepaid_component_is_priced_for_the_whole_period(self):
        requested_resource = self._request(
            (datetime.date.today() + datetime.timedelta(days=180)).isoformat()
        )

        resource = self._allocate(requested_resource)

        # 1000 hours x 2.50 x 6 months, not a single month's 2500.
        self.assertEqual(resource.cost, CPU_PRICE * CPU_HOURS * MONTHS)

    def test_a_request_without_a_period_is_left_open(self):
        requested_resource = self._request(None)

        resource = self._allocate(requested_resource)

        self.assertIsNone(resource.end_date)
        self.assertEqual(resource.cost, CPU_PRICE * CPU_HOURS)

    def test_review_outlasting_the_period_still_grants_it_in_full(self):
        # The stored date is absolute and computed when the request was
        # drafted. A proposal reviewed for longer than the period it asked for
        # carries a date in the past, which validate_end_date rejects; the
        # length is what must survive.
        requested_resource = self._request(
            (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        )
        requested_resource.created = requested_resource.created - datetime.timedelta(
            days=210
        )
        requested_resource.save(update_fields=["created"])

        resource = self._allocate(requested_resource)

        self.assertIsNotNone(resource.end_date)
        self.assertGreater(resource.end_date, datetime.date.today())
        self.assertEqual(resource.cost, CPU_PRICE * CPU_HOURS * MONTHS)

    def test_a_period_the_offering_refuses_leaves_the_resource_open(self):
        # Allocation must not fail over a date: the resource is left open and
        # the operator is warned.
        self.offering.plugin_options = {"max_resource_termination_offset_in_days": 30}
        self.offering.save()
        requested_resource = self._request(
            (datetime.date.today() + datetime.timedelta(days=180)).isoformat()
        )

        with mock.patch("waldur_mastermind.proposal.utils.logger") as mock_logger:
            resource = self._allocate(requested_resource)

        self.assertIsNone(resource.end_date)
        self.assertTrue(mock_logger.warning.called)

    def test_the_stored_length_outranks_the_end_date_beside_it(self):
        # The form re-anchors end_date on the day the request is edited, while
        # this measured it from the day the request was created: reopening a
        # 45-day-old draft and saving it unchanged used to stretch a two-month
        # subscription to four.
        requested_resource = self._age(
            self._request(
                (datetime.date.today() + relativedelta(months=2)).isoformat(),
                prepaid_duration_months=2,
            ),
            45,
        )

        resource = self._allocate(requested_resource)

        self.assertEqual(
            resource.end_date, datetime.date.today() + relativedelta(months=2)
        )
        self.assertEqual(resource.cost, CPU_PRICE * CPU_HOURS * 2)

    def test_no_age_of_draft_reaches_the_granted_period(self):
        # Straight at the helper: allocation runs once per proposal, and what
        # matters here is that the row's age has stopped being an input at all.
        for age in (0, 1, 45, 90):
            with self.subTest(age=age):
                requested_resource = self._age(
                    self._request(None, prepaid_duration_months=MONTHS), age
                )

                self.assertEqual(
                    utils._requested_end_date(
                        requested_resource,
                        self.fixture.proposal_project,
                        datetime.date.today(),
                    ),
                    datetime.date.today() + relativedelta(months=MONTHS),
                )

    def test_an_unusable_length_falls_back_to_the_end_date(self):
        requested_resource = self._request(
            (datetime.date.today() + datetime.timedelta(days=180)).isoformat(),
            prepaid_duration_months=0,
        )

        with mock.patch("waldur_mastermind.proposal.utils.logger") as mock_logger:
            resource = self._allocate(requested_resource)

        self.assertEqual(
            resource.end_date, datetime.date.today() + relativedelta(months=MONTHS)
        )
        self.assertTrue(mock_logger.warning.called)


class ProjectDurationTest(PrepaidFixture, test.APITestCase):
    """The allocated project runs for as long as what it holds."""

    def _derived(self):
        return utils.project_end_date(self.proposal, datetime.date.today())

    def test_the_longest_subscription_sets_the_project_when_the_call_fixes_none(
        self,
    ):
        self.fixture.call.fixed_duration_in_days = None
        self.fixture.call.save()
        self._request(None, prepaid_duration_months=3)
        self._request(None, prepaid_duration_months=12)
        self._request(None, prepaid_duration_months=6)

        self.assertEqual(
            self._derived(), datetime.date.today() + relativedelta(months=12)
        )

    def test_a_proposal_asking_for_no_subscription_falls_back_to_the_call(self):
        # A call may accept prepaid and non-prepaid offerings side by side, so a
        # proposal that requested only the latter still needs a duration.
        self.fixture.call.fixed_duration_in_days = 90
        self.fixture.call.save()

        self.assertEqual(
            self._derived(), datetime.date.today() + datetime.timedelta(days=90)
        )

    def test_the_call_caps_the_subscription(self):
        # The fixed duration is the length of every project the call awards,
        # so a subscription under it cannot stretch the project. The two units
        # are never converted into each other; each is resolved against the
        # same date instead.
        self.fixture.call.fixed_duration_in_days = 90
        self.fixture.call.save()
        self._request(None, prepaid_duration_months=2)

        self.assertEqual(
            self._derived(), datetime.date.today() + datetime.timedelta(days=90)
        )

    def test_a_subscription_under_a_fixed_call_ends_within_the_project(self):
        self.fixture.call.fixed_duration_in_days = 90
        self.fixture.call.save()
        requested_resource = self._request(None, prepaid_duration_months=2)

        utils.allocate_proposal(self.proposal)
        self.proposal.refresh_from_db()
        requested_resource.refresh_from_db()

        today = datetime.date.today()
        self.assertEqual(
            self.proposal.project.end_date, today + datetime.timedelta(days=90)
        )
        self.assertEqual(
            requested_resource.resource.end_date, today + relativedelta(months=2)
        )
        self.assertLessEqual(
            requested_resource.resource.end_date, self.proposal.project.end_date
        )

    def test_a_subscription_outlasting_a_fixed_call_is_clamped_to_the_project(self):
        # The serializer rejects such a request; a row that slipped past it is
        # still shortened to the project rather than left outlasting it.
        self.fixture.call.fixed_duration_in_days = 90
        self.fixture.call.save()
        requested_resource = self._request(None, prepaid_duration_months=12)

        utils.allocate_proposal(self.proposal)
        self.proposal.refresh_from_db()
        requested_resource.refresh_from_db()

        self.assertEqual(
            self.proposal.project.end_date,
            datetime.date.today() + datetime.timedelta(days=90),
        )
        self.assertEqual(
            requested_resource.resource.end_date, self.proposal.project.end_date
        )

    def test_no_duration_anywhere_leaves_the_project_open(self):
        self.fixture.call.fixed_duration_in_days = None
        self.fixture.call.save()

        self.assertIsNone(self._derived())

    def test_a_length_on_an_offering_that_sells_none_is_not_a_subscription(self):
        # The attribute alone means nothing: only an offering with a prepaid
        # component is bought by the month.
        self.cpu.is_prepaid = False
        self.cpu.save()
        self._request(None, prepaid_duration_months=12)

        self.assertIsNone(utils.get_proposal_duration_months(self.proposal))

    def test_the_project_gets_its_end_date_at_allocation(self):
        self._request(None, prepaid_duration_months=MONTHS)

        utils.allocate_proposal(self.proposal)
        self.proposal.refresh_from_db()

        self.assertEqual(
            self.proposal.project.end_date,
            datetime.date.today() + relativedelta(months=MONTHS),
        )

    def test_a_resource_is_never_left_outlasting_its_project(self):
        # The project's end date caps the resource's rather than rejecting it.
        # A rejection would leave the resource with no end date at all, and a
        # prepaid resource with no end date is invoiced for a single month.
        requested_resource = self._request(None, prepaid_duration_months=MONTHS)

        utils.allocate_proposal(self.proposal)
        self.proposal.refresh_from_db()
        requested_resource.refresh_from_db()

        self.assertIsNotNone(requested_resource.resource.end_date)
        self.assertLessEqual(
            requested_resource.resource.end_date, self.proposal.project.end_date
        )

    def _schedule_allocation(self, days_ahead):
        """Date the round's allocation forward, as a fixed_date call does."""
        from waldur_mastermind.proposal.enums import AllocationTimes

        proposal_round = self.proposal.round
        proposal_round.allocation_date = timezone.now() + datetime.timedelta(
            days=days_ahead
        )
        proposal_round.save()
        models.CallWorkflowStep.objects.update_or_create(
            call=proposal_round.call,
            step="allocation_decision",
            defaults={"allocation_time": AllocationTimes.FIXED_DATE},
        )
        return proposal_round.allocation_date.date()

    def test_the_period_starts_when_allocation_is_scheduled_for(self):
        # Approved today, allocated in four months: the grant used to expire
        # MONTHS after the decision rather than MONTHS after it could be used.
        start = self._schedule_allocation(120)
        requested_resource = self._request(None, prepaid_duration_months=MONTHS)

        utils.allocate_proposal(self.proposal)
        self.proposal.refresh_from_db()
        requested_resource.refresh_from_db()

        self.assertEqual(self.proposal.project.start_date, start)
        self.assertEqual(
            requested_resource.resource.end_date, start + relativedelta(months=MONTHS)
        )

    def test_each_subscription_keeps_its_own_length(self):
        # Two prepaid requests of different lengths on one proposal: each
        # resource expires on its own date, and the project covers the longest.
        # The shorter one is not stretched to the project, nor the longer one
        # cut down to the shorter.
        start = self._schedule_allocation(120)
        shorter = self._request(None, prepaid_duration_months=2)
        longer = self._request(None, prepaid_duration_months=5)

        utils.allocate_proposal(self.proposal)
        self.proposal.refresh_from_db()
        shorter.refresh_from_db()
        longer.refresh_from_db()

        self.assertEqual(shorter.resource.end_date, start + relativedelta(months=2))
        self.assertEqual(longer.resource.end_date, start + relativedelta(months=5))
        self.assertEqual(
            self.proposal.project.end_date, start + relativedelta(months=5)
        )
        # The project outlives the shorter subscription rather than ending with
        # it, so the longer one is not terminated early by the project sweep.
        self.assertGreater(self.proposal.project.end_date, shorter.resource.end_date)
        # And each is priced for what it asked for, not for the pair.
        self.assertEqual(shorter.resource.cost, CPU_PRICE * CPU_HOURS * 2)
        self.assertEqual(longer.resource.cost, CPU_PRICE * CPU_HOURS * 5)

    def test_a_call_that_allocates_on_decision_is_unchanged(self):
        requested_resource = self._request(None, prepaid_duration_months=MONTHS)

        utils.allocate_proposal(self.proposal)
        requested_resource.refresh_from_db()

        self.assertIsNone(self.proposal.project.start_date)
        self.assertEqual(
            requested_resource.resource.end_date,
            datetime.date.today() + relativedelta(months=MONTHS),
        )

    def test_a_termination_offset_is_measured_from_the_same_anchor(self):
        # The offering allows six months from the start; the grant is six months
        # from a start four months out. Measuring the offset from today instead
        # would reject the date, and a rejection leaves the resource with none —
        # which invoices a prepaid component for a single month.
        self._schedule_allocation(120)
        self.offering.plugin_options = {"max_resource_termination_offset_in_days": 190}
        self.offering.save()
        requested_resource = self._request(None, prepaid_duration_months=MONTHS)

        utils.allocate_proposal(self.proposal)
        requested_resource.refresh_from_db()

        self.assertIsNotNone(requested_resource.resource.end_date)
