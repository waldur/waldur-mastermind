from ddt import ddt, data
from rest_framework import test, status

from . import factories, fixtures


@ddt
class PackageTemplateListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.package_template = self.fixture.openstack_template
        self.url = factories.PackageTemplateFactory.get_list_url()

    @data('staff', 'owner', 'manager')
    def test_user_can_list_package_templates(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_user_can_not_list_package_templates(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)


@ddt
class PackageTemplateRetreiveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.package_template = self.fixture.openstack_template
        self.url = factories.PackageTemplateFactory.get_url(self.package_template)

    @data('staff', 'owner', 'manager')
    def test_user_can_retrieve_package_template(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['uuid'], self.package_template.uuid.hex)

    def test_user_can_not_retrieve_package_template(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
