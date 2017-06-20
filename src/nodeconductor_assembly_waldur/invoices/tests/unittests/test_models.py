from django.test import TestCase
from freezegun import freeze_time

from .. import fixtures


class OpenStackItemTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_usage_days_cannot_be_larger_than_end_field(self):
        with freeze_time('2016-11-17 14:00:00'):
            items = self.fixture.invoice.openstack_items.all()

        with freeze_time('2016-12-1 14:00:00'):
            for item in items:
                self.assertEqual(item.usage_days, item.end)
