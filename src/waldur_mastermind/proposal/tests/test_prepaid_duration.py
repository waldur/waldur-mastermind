"""The subscription period an applicant requests must survive allocation.

The resource request stores it as ``attributes.end_date``; the prepaid
multiplier in ``Plan.get_estimate`` and in the invoice item builder both read
``Resource.end_date``. Allocation used to drop the value in between, so a
six-month grant was priced and invoiced as one month.
"""

import datetime
from decimal import Decimal
from unittest import mock

from rest_framework import test

from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import utils
from waldur_mastermind.proposal.tests import factories, fixtures

CPU_PRICE = Decimal("2.50")
CPU_HOURS = 1000
MONTHS = 6


class PrepaidDurationTest(test.APITestCase):
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

    def _request(self, end_date):
        attributes = {"end_date": end_date} if end_date else {}
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
