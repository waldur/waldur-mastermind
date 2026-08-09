"""Renewals that complete asynchronously must still write their end date."""

from datetime import timedelta

from django.utils import timezone
from rest_framework import test

from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace.enums import (
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class AsynchronousRenewalEndDateTest(test.APITestCase):
    """Regression: renewals completing asynchronously never wrote their end date.

    Their end date was applied only in the synchronous branch of the processor,
    so offerings whose update_limits_process returns False (site agent, support,
    openportal, remote) marked the order done with the resource untouched. The
    write now lives in resource_update_succeeded, which every route ends at.
    """

    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.initial_end_date = timezone.now().date() + timedelta(days=30)
        self.new_end_date = self.initial_end_date + timedelta(days=180)
        self.resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            state=ResourceStates.UPDATING,
            limits={"cpu_hours": 2},
            end_date=self.initial_end_date,
        )
        self.order = factories.OrderFactory(
            resource=self.resource,
            project=self.fixture.project,
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            limits={"cpu_hours": 2},
            attributes={
                "action": "renew",
                "old_limits": {"cpu_hours": 2},
                "old_end_date": self.initial_end_date.isoformat(),
                "new_end_date": self.new_end_date.isoformat(),
                "extension_months": 6,
            },
        )

    def test_end_date_is_applied_on_the_callback_route(self):
        callbacks.resource_update_succeeded(self.resource)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, self.new_end_date)
