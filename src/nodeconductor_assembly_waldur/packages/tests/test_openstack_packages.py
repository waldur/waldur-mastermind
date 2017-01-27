from ddt import ddt, data
from rest_framework import test, status
from rest_framework.reverse import reverse

from nodeconductor.structure import models as structure_models
from nodeconductor_openstack.openstack import models as openstack_models

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
            'template': factories.PackageTemplateFactory.get_url(template),
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
        self.assertDictEqual(tenant.extra_configuration, {
            'package_name': template.name,
            'package_uuid': template.uuid.hex,
            'package_category': template.get_category_display(),
        })


@ddt
class OpenStackPackageExtendTest(test.APITransactionTestCase):
    extend_url = factories.OpenStackPackageFactory.get_list_url(action='extend')

    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.package = self.fixture.openstack_package
        self.new_template = factories.PackageTemplateFactory(service_settings=self.fixture.openstack_service_settings)

    @data('staff', 'owner', 'manager')
    def test_can_extend_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.extend_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('admin', 'user')
    def test_cannot_extend_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.extend_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_package_is_replaced_on_extend(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.extend_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        old_package = models.OpenStackPackage.objects.filter(uuid=self.package.uuid)
        self.assertFalse(old_package.exists())

        new_package = models.OpenStackPackage.objects.filter(template=self.new_template)
        self.assertTrue(new_package.exists())

    def test_user_cannot_extend_package_with_tenant_in_invalid_state(self):
        self.package.tenant.state = openstack_models.Tenant.States.ERRED
        self.package.tenant.save(update_fields=['state'])

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.extend_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['package'], ["Package's tenant must be in OK state."])

    def test_user_cannot_extend_package_with_new_template_settings_in_invalid_state(self):
        self.new_template.service_settings.state = structure_models.ServiceSettings.States.ERRED
        self.new_template.service_settings.save(update_fields=['state'])

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.extend_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['template'], ["Template's settings must be in OK state."])

    def test_user_cannot_extend_package_with_same_template(self):
        self.client.force_authenticate(user=self.fixture.staff)

        payload = self.get_valid_payload(template=self.package.template)
        response = self.client.post(self.extend_url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'],
                         ["New package template cannot be the same as package's current template."])

    @data('ram', 'cores', 'storage')
    def test_user_cannot_extend_package_with_smaller_template_component(self, component_type):
        self.client.force_authenticate(user=self.fixture.staff)

        old_component = self.package.template.components.get(type=component_type)
        old_component.amount = 5
        old_component.save(update_fields=['amount'])

        new_component = self.new_template.components.get(type=component_type)
        new_component.amount = 4
        new_component.save(update_fields=['amount'])
        payload = self.get_valid_payload()
        response = self.client.post(self.extend_url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_after_package_extension_tenant_is_updated(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.extend_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.package.tenant.refresh_from_db()
        self.assertDictEqual(self.package.tenant.extra_configuration, {
            'package_name': self.new_template.name,
            'package_uuid': self.new_template.uuid.hex,
            'package_category': self.new_template.get_category_display(),
        })

    # Helper methods
    def get_valid_payload(self, template=None, package=None):
        return {
            'template': factories.PackageTemplateFactory.get_url(template or self.new_template),
            'package': factories.OpenStackPackageFactory.get_url(package or self.package),
        }
