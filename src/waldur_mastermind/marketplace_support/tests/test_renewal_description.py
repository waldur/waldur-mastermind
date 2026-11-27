from decimal import Decimal

from rest_framework import test

from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_support import utils


class RenewalDescriptionTest(test.APITestCase):
    def _order(self, lines):
        resource = factories.ResourceFactory(limits={"storage": 50})
        return factories.OrderFactory(
            resource=resource,
            offering=resource.offering,
            type=OrderTypes.UPDATE,
            limits={"storage": 120},
            cost=Decimal("5832.00"),
            attributes={
                "action": "renew",
                "extension_months": 12,
                "old_limits": {"storage": 50},
                "cost_lines": lines,
            },
        )

    def test_a_quoted_discount_is_named(self):
        description = utils.format_renewal_description(
            self._order(
                [{"total": "6480", "discount_total": "648", "discount_deferred": False}]
            )
        )

        self.assertIn("Renewal cost: 5832.00", description)
        self.assertIn(
            "Volume discount: 648.00, included in the renewal cost", description
        )

    def test_a_deferred_discount_is_noted(self):
        description = utils.format_renewal_description(
            self._order(
                [{"total": "6480", "discount_total": "0", "discount_deferred": True}]
            )
        )

        self.assertIn("Volume discount: applied on the invoice", description)

    def test_no_discount_adds_no_line(self):
        description = utils.format_renewal_description(self._order([{"total": "6480"}]))

        self.assertNotIn("Volume discount", description)
