from ddt import ddt, data
from rest_framework import test, status

from . import factories, fixtures


@ddt
class PackageTemplateListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.url = factories.PackageTemplateFactory.get_list_url()

    @data('staff', 'owner', 'manager', 'admin', 'user')
    def test_user_can_list_package_templates(self, user):
        package_template = self.fixture.openstack_template

        url = factories.PackageTemplateFactory.get_list_url()
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)

        self.assertEqual(len(response.data), 1)

    @data('staff', 'owner', 'manager', 'admin', 'user')
    def test_archived_templates_are_not_shown(self, user):
        factories.PackageTemplateFactory(archived=True)
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)


@ddt
class PackageTemplateRetreiveTest(test.APITransactionTestCase):
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
