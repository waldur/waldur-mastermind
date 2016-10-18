from decimal import Decimal
import random

from django.test import TestCase

from .. import factories
from ... import models


class PackageTemplateTest(TestCase):

    def test_package_price_is_based_on_components(self):
        package_template = factories.PackageTemplateFactory()
        total = Decimal('0.00')
        for t in models.PackageTemplate.get_required_component_types():
            amount = random.randint(1, 10)
            price = Decimal('4.95')
            factories.PackageComponentFactory(template=package_template, type=t, amount=amount, price=price)
            total += amount * price

        self.assertEqual(package_template.price, total)
