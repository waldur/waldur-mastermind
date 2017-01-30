from django.test import TestCase

from .. import factories
from ... import admin, models


class TestPackageComponentForm(TestCase):

    def test_package_component_form_is_valid_when_component_price_is_0(self):
        data = {
            'monthly_price': '0',
            'amount': '2',
            'type': models.PackageComponent.Types.RAM,
            'price': '9', # price is required but not used in form validation.
        }
        form = admin.PackageComponentForm(data=data)
        self.assertTrue(form.is_valid())

    def test_package_component_form_is_invalid_if_package_template_has_connected_packages_already(self):
        template = factories.PackageTemplateFactory()
        factories.OpenStackPackageFactory(template=template)
        instance = template.components.first()

        data = {
            'monthly_price': '0',
            'amount': '2',
            'type': instance.type,
            'price': '9', # price is required but not used in form validation.
        }
        form = admin.PackageComponentForm(data=data, instance=instance)
        self.assertFalse(form.is_valid())

