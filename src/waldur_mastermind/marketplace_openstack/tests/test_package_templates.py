from decimal import Decimal

from rest_framework import test

from waldur_mastermind.marketplace_openstack.tests import fixtures


class PackageTemplatePriceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.package_template = self.fixture.openstack_template
        prices = (Decimal('1.411'), Decimal('2.224'), Decimal('3.312'))
        self.daily_price = Decimal('6.95')
        self.monthly_price = Decimal('208.50')
        for price, component in zip(
            prices, list(self.package_template.components.all())
        ):
            component.price = price
            component.save()

    def test_daily_price_is_rounded_to_2_places_after_decimal_point(self):
        self.assertEqual(self.package_template.price, self.daily_price)

    def test_monthly_price_is_rounded_to_2_places_after_decimal_point(self):
        self.assertEqual(self.package_template.monthly_price, self.monthly_price)
