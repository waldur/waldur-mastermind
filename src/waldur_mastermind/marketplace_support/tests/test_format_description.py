import datetime

from django.test import TestCase

from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import utils


class FormatCreateDescriptionTest(TestCase):
    def test_start_date_included_when_set(self):
        order = marketplace_factories.OrderFactory(
            start_date=datetime.date(2026, 6, 1),
        )
        description = utils.format_create_description(order)
        self.assertIn("Start date: June 1, 2026", description)

    def test_end_date_included_when_set(self):
        order = marketplace_factories.OrderFactory()
        order.resource.end_date = datetime.date(2026, 12, 31)
        order.resource.save(update_fields=["end_date"])
        description = utils.format_create_description(order)
        self.assertIn("End date: Dec. 31, 2026", description)

    def test_dates_included_when_both_set(self):
        order = marketplace_factories.OrderFactory(
            start_date=datetime.date(2026, 6, 1),
        )
        order.resource.end_date = datetime.date(2026, 12, 31)
        order.resource.save(update_fields=["end_date"])
        description = utils.format_create_description(order)
        self.assertIn("Start date:", description)
        self.assertIn("End date:", description)

    def test_dates_not_included_when_not_set(self):
        order = marketplace_factories.OrderFactory(
            start_date=None,
        )
        order.resource.end_date = None
        order.resource.save(update_fields=["end_date"])
        description = utils.format_create_description(order)
        self.assertNotIn("Start date:", description)
        self.assertNotIn("End date:", description)

    def test_restoration_note_included_for_restore_order(self):
        order = marketplace_factories.OrderFactory(type=OrderTypes.RESTORE)
        description = utils.format_create_description(order)
        self.assertIn(
            "This is a restoration request for a previously terminated resource.",
            description,
        )

    def test_restoration_note_not_included_for_create_order(self):
        order = marketplace_factories.OrderFactory(type=OrderTypes.CREATE)
        description = utils.format_create_description(order)
        self.assertNotIn("restoration request", description)
