from ddt import ddt, data
from rest_framework import test, status

from nodeconductor.structure.tests import factories as structure_factories

from . import PackageTemplateFactory


@ddt
class PackageTemplateListTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()

    @data('staff', 'user')
    def test_user_can_list_package_templates(self, user):
        self.client.force_authenticate(user=getattr(self, user))

        response = self.client.get(PackageTemplateFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class PackageTemplateRetreiveTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()

    @data('staff', 'user')
    def test_user_can_retrieve_package_template(self, user):
        package_template = PackageTemplateFactory()
        self.client.force_authenticate(user=getattr(self, user))

        response = self.client.get(PackageTemplateFactory.get_url(package_template))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
