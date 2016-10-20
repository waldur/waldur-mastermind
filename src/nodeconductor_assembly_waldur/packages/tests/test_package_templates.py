from ddt import ddt, data
from rest_framework import test, status

from . import factories, fixtures


@ddt
class PackageTemplateListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()

    @data('staff', 'user')
    def test_user_can_list_package_templates(self, user):
        package_template = self.fixture.openstack_template
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(factories.PackageTemplateFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@ddt
class PackageTemplateRetreiveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()

    @data('staff', 'user')
    def test_user_can_retrieve_package_template(self, user):
        package_template = self.fixture.openstack_template
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(factories.PackageTemplateFactory.get_url(package_template))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['uuid'], package_template.uuid.hex)
