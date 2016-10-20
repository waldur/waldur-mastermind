from ddt import ddt, data
from rest_framework import test, status
from rest_framework.reverse import reverse

from . import factories, fixtures
from .. import models


@ddt
class OpenStackPackageRetreiveTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.PackageFixture()

    def get_package(self):
        openstack_package = self.fixture.openstack_package
        return self.client.get(factories.OpenStackPackageFactory.get_url(openstack_package))

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_retrieve_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.get_package()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_user_cannot_retrieve_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.get_package()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class OpenStackPackageCreateTest(test.APITransactionTestCase):
    url = factories.OpenStackPackageFactory.get_list_url()

    def setUp(self):
        self.fixture = fixtures.PackageFixture()

    def get_valid_payload(self):
        spl = self.fixture.openstack_spl
        spl_url = 'http://testserver' + reverse('openstack-spl-detail', kwargs={'pk': spl.pk})
        template = factories.PackageTemplateFactory(service_settings=spl.service.settings)
        return {
            'service_project_link': spl_url,
            'name': 'test_package',
            'template': factories.PackageTemplateFactory.get_url(template)
        }

    @data('staff', 'owner', 'manager')
    def test_user_can_create_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('user', 'admin')
    def test_user_cannot_create_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_tenant_quotas_are_defined_by_template(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(self.url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        package = models.OpenStackPackage.objects.get(uuid=response.data['uuid'])
        tenant, template = package.tenant, package.template
        for quota_name, component_type in models.OpenStackPackage.get_quota_to_component_mapping().items():
            self.assertEqual(
                tenant.quotas.get(name=quota_name).limit, template.components.get(type=component_type).amount)

    def test_template_data_is_saved_tenant_extra_configurations(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(self.url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        package = models.OpenStackPackage.objects.get(uuid=response.data['uuid'])
        tenant, template = package.tenant, package.template
        self.assertDictEqual(
            tenant.extra_configuration, {'package_name': template.name, 'package_uuid': template.uuid.hex})
