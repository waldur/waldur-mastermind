import random

from ddt import ddt, data
from decimal import Decimal

from rest_framework import test, status

from nodeconductor.structure.tests import factories as structure_factories

from nodeconductor_assembly_waldur.packages import models
from nodeconductor_assembly_waldur.packages.tests.factories import PackageTemplateFactory, PackageComponentFactory


@ddt
class PackageTemplatePermissionModelTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()

    # Permission tests
    @data('staff', 'user')
    def test_user_can_list_package_templates(self, user):
        self.client.force_authenticate(user=getattr(self, user))

        response = self.client.get(PackageTemplateFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('staff', 'user')
    def test_user_can_retrieve_package_template(self, user):
        package_template = PackageTemplateFactory()
        self.client.force_authenticate(user=getattr(self, user))

        response = self.client.get(PackageTemplateFactory.get_url(package_template))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('staff', 'user')
    def test_user_cannot_create_package_template(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_template_payload()

        response = self.client.post(PackageTemplateFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @data('staff', 'user')
    def test_user_cannot_update_package_template(self, user):
        package_template = PackageTemplateFactory()
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_template_payload(package_template)

        response = self.client.put(PackageTemplateFactory.get_url(package_template), data=payload)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @data('staff', 'user')
    def test_user_cannot_delete_package_template(self, user):
        package_template = PackageTemplateFactory()
        self.client.force_authenticate(user=getattr(self, user))

        response = self.client.delete(PackageTemplateFactory.get_url(package_template))
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    # Model tests
    def test_package_price_is_based_on_components(self):
        package_template = PackageTemplateFactory()
        total = Decimal('0.00')
        for t in models.PackageTemplate.get_required_component_types():
            amount = random.randint(1, 10)
            price = Decimal('4.95')
            PackageComponentFactory(template=package_template, type=t, amount=amount, price=price)
            total += amount * price

        self.assertEqual(package_template.price, total)

    # Helper methods
    def _get_valid_template_payload(self, package_template=None):
        package_template = package_template or PackageTemplateFactory.build()
        payload = {
            'name': package_template.name,
            'type': package_template.type,
            'description': package_template.description,
            'icon_url': package_template.icon_url,
            'components': []
        }
        for component_type in models.PackageTemplate.get_required_component_types():
            if not package_template.components.filter(type=component_type).exists():
                package_component = PackageComponentFactory.build(type=component_type, template=package_template)
                payload['components'].append(self._get_valid_component_payload(package_component))

        return payload

    def _get_valid_component_payload(self, package_component=None):
        package_component = package_component or PackageComponentFactory.build()
        return {
            'type': package_component.type,
            'amount': package_component.amount,
            'price': package_component.price,
        }
