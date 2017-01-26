from decimal import Decimal
import random

from django.test import TestCase
from django.db.models import ProtectedError

from .. import factories
from ... import models


class PackageTemplateTest(TestCase):

    def test_package_price_is_based_on_components(self):
        package_template = factories.PackageTemplateFactory(components=[])
        total = Decimal('0.00')
        for t in models.PackageTemplate.get_required_component_types():
            component = package_template.components.get(type=t)
            component.amount = random.randint(1, 10)
            component.price = Decimal('4.95')
            component.save()
            total += component.amount * component.price

        self.assertEqual(package_template.price, total)

    def test_template_cannot_be_deleted_if_has_linked_packaged(self):
        package_template = factories.PackageTemplateFactory(components=[])
        factories.OpenStackPackageFactory(template=package_template)

        with self.assertRaises(ProtectedError):
            package_template.delete()
