from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.packages import admin, models
from waldur_mastermind.packages.tests import factories, fixtures

User = get_user_model()


class PackageTemplateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='super',
            password='secret',
            email='super@example.com',
            is_staff=True,
        )

        cls.add_url = reverse('admin:packages_packagetemplate_add')

        cls.fixture = fixtures.OpenStackFixture()
        cls.service_settings = cls.fixture.openstack_service_settings

        cls.inline_post_data = {
            'name': 'TEST',
            'category': 'small',
            'service_settings': cls.service_settings.id,
            'unit': common_mixins.UnitPriceMixin.Units.PER_DAY,
            '_continue': '1',
            'components-TOTAL_FORMS': 3,
            'components-INITIAL_FORMS': 0,
            'components-MIN_NUM_FORMS': 0,
            'components-MAX_NUM_FORMS': 1000,
            'components-0-id': '',
            'components-0-template': '',
            'components-0-type': 'ram',
            'components-0-amount': 1,
            'components-0-monthly_price': 1,
            'components-1-id': '',
            'components-1-template': '',
            'components-1-type': 'cores',
            'components-1-amount': 2,
            'components-1-monthly_price': 2,
            'components-2-id': '',
            'components-2-template': '',
            'components-2-type': 'storage',
            'components-2-amount': 3,
            'components-2-monthly_price': 3,
        }

    def setUp(self):
        self.client.force_login(self.staff)

    def test_create_package_template(self):
        response = self.client.post(self.add_url, self.inline_post_data)
        self.assertEqual(response.status_code, 302)


class TestPackageComponentForm(TestCase):
    def test_package_component_form_is_valid_when_component_price_is_0(self):
        data = {
            'monthly_price': '0',
            'amount': '2',
            'type': models.PackageComponent.Types.RAM,
            'price': '9',  # price is required but not used in form validation.
        }
        form = admin.PackageComponentForm(data=data)
        self.assertTrue(form.is_valid())

    def test_package_component_form_is_invalid_if_package_template_has_connected_packages_already(
        self,
    ):
        template = factories.PackageTemplateFactory()
        factories.OpenStackPackageFactory(template=template)
        instance = template.components.first()

        data = {
            'monthly_price': '0',
            'amount': '2',
            'type': instance.type,
            'price': '9',  # price is required but not used in form validation.
        }
        form = admin.PackageComponentForm(data=data, instance=instance)
        self.assertFalse(form.is_valid())
