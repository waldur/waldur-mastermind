from ddt import ddt, data
from decimal import Decimal
from rest_framework import test, status

from . import factories, fixtures


@ddt
class PackageTemplateListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.url = factories.PackageTemplateFactory.get_list_url()

    @data('staff', 'owner', 'manager', 'admin', 'user')
    def test_user_can_list_package_templates(self, user):
        self.fixture.openstack_template

        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)

        self.assertEqual(len(response.data), 1)


@ddt
class PackageTemplateRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.package_template = self.fixture.openstack_template
        self.url = factories.PackageTemplateFactory.get_url(self.package_template)

    @data('staff', 'owner', 'manager', 'admin', 'user')
    def test_user_can_retrieve_package_template(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['uuid'], self.package_template.uuid.hex)


class PackageTemplatePriceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.package_template = self.fixture.openstack_template
        prices = (Decimal('1.411'), Decimal('2.224'), Decimal('3.312'))
        self.daily_price = Decimal('6.95')
        self.monthly_price = Decimal('208.50')
        for price, component in zip(prices, list(self.package_template.components.all())):
            component.price = price
            component.save()

    def test_daily_price_is_rounded_to_2_places_after_decimal_point(self):
        self.assertEqual(self.package_template.price, self.daily_price)

    def test_monthly_price_is_rounded_to_2_places_after_decimal_point(self):
        self.assertEqual(self.package_template.monthly_price, self.monthly_price)
