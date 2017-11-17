from ddt import ddt, data
from django.conf import settings
from rest_framework import test, status
from rest_framework.reverse import reverse

from nodeconductor.structure import models as structure_models
from nodeconductor_openstack.openstack import models as openstack_models
from nodeconductor_openstack.openstack.tests import factories as openstack_factories

from . import factories, fixtures
from .. import models


@ddt
class OpenStackPackageRetrieveTest(test.APITransactionTestCase):

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
        spl_url = factories.OpenStackServiceProjectLinkFactory.get_url(spl)
        template = factories.PackageTemplateFactory(service_settings=spl.service.settings)
        return {
            'service_project_link': spl_url,
            'name': 'test_package',
            'template': factories.PackageTemplateFactory.get_url(template),
        }

    @data('staff', 'owner')
    def test_user_can_create_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('user', 'admin')
    def test_user_cannot_create_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manager_can_create_openstack_package_with_permission_from_settings(self):
        openstack_settings = settings.NODECONDUCTOR_OPENSTACK.copy()
        openstack_settings['MANAGER_CAN_MANAGE_TENANTS'] = True
        self.client.force_authenticate(user=self.fixture.manager)

        with self.settings(NODECONDUCTOR_OPENSTACK=openstack_settings):
            response = self.client.post(self.url, data=self.get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
            'cores': template.components.get(type='cores').amount,
            'ram': template.components.get(type='ram').amount,
            'storage': template.components.get(type='storage').amount,
        })

    def test_user_cannot_create_openstack_package_if_template_is_archived(self):
        self.client.force_authenticate(user=self.fixture.owner)
        spl = self.fixture.openstack_spl
        spl_url = 'http://testserver' + reverse('openstack-spl-detail', kwargs={'pk': spl.pk})
        template = factories.PackageTemplateFactory(archived=True,
                                                    service_settings=spl.service.settings)
        payload = {
            'service_project_link': spl_url,
            'name': 'test_package',
            'template': factories.PackageTemplateFactory.get_url(template),
        }

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class OpenStackPackageChangeTest(test.APITransactionTestCase):
    change_url = factories.OpenStackPackageFactory.get_list_url(action='change')

    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.package = self.fixture.openstack_package
        self.new_template = factories.PackageTemplateFactory(service_settings=self.fixture.openstack_service_settings)

    @data('staff', 'owner')
    def test_can_extend_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.change_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('admin', 'user')
    def test_cannot_extend_openstack_package(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.change_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manager_can_extend_openstack_package_with_permission_from_settings(self):
        openstack_settings = settings.NODECONDUCTOR_OPENSTACK.copy()
        openstack_settings['MANAGER_CAN_MANAGE_TENANTS'] = True
        self.client.force_authenticate(user=self.fixture.manager)

        with self.settings(NODECONDUCTOR_OPENSTACK=openstack_settings):
            response = self.client.post(self.change_url, data=self.get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_package_is_replaced_on_extend(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.change_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        old_package = models.OpenStackPackage.objects.filter(uuid=self.package.uuid)
        self.assertFalse(old_package.exists())

        new_package = models.OpenStackPackage.objects.filter(template=self.new_template)
        self.assertTrue(new_package.exists())

    def test_user_cannot_extend_package_with_tenant_in_invalid_state(self):
        self.package.tenant.state = openstack_models.Tenant.States.ERRED
        self.package.tenant.save(update_fields=['state'])

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.change_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['package'], ["Package's tenant must be in OK state."])

    def test_user_cannot_extend_package_with_new_template_settings_in_invalid_state(self):
        self.new_template.service_settings.state = structure_models.ServiceSettings.States.ERRED
        self.new_template.service_settings.save(update_fields=['state'])

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.change_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['template'], ["Template's settings must be in OK state."])

    def test_user_cannot_extend_package_with_same_template(self):
        self.client.force_authenticate(user=self.fixture.staff)

        payload = self.get_valid_payload(template=self.package.template)
        response = self.client.post(self.change_url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'],
                         ["New package template cannot be the same as package's current template."])

    @data('ram', 'cores', 'storage')
    def test_user_cannot_decrease_package_if_tenant_usage_exceeds_new_limits(self, component_type):
        self.set_usage_and_limit(component_type, usage=10, old_limit=20, new_limit=5)

        self.client.force_authenticate(user=self.fixture.staff)
        payload = self.get_valid_payload()
        response = self.client.post(self.change_url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('ram', 'cores', 'storage')
    def test_user_can_decrease_package_if_tenant_usage_does_not_exceeds_new_limits(self, component_type):
        self.set_usage_and_limit(component_type, usage=5, old_limit=20, new_limit=5)

        self.client.force_authenticate(user=self.fixture.staff)
        payload = self.get_valid_payload()
        response = self.client.post(self.change_url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_after_package_extension_tenant_is_updated(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.change_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.package.tenant.refresh_from_db()
        self.assertDictEqual(self.package.tenant.extra_configuration, {
            'package_name': self.new_template.name,
            'package_uuid': self.new_template.uuid.hex,
            'package_category': self.new_template.get_category_display(),
            'cores': self.new_template.components.get(type='cores').amount,
            'ram': self.new_template.components.get(type='ram').amount,
            'storage': self.new_template.components.get(type='storage').amount,
        })

    def test_after_package_extension_related_service_settings_are_updated(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.change_url, data=self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        quotas = self.package.service_settings.quotas
        components = self.new_template.components
        self.assertEqual(quotas.get(name='vcpu').limit, components.get(type='cores').amount)
        self.assertEqual(quotas.get(name='ram').limit, components.get(type='ram').amount)
        self.assertEqual(quotas.get(name='storage').limit, components.get(type='storage').amount)

    def set_usage_and_limit(self, component_type, usage, old_limit, new_limit):
        mapping = models.OpenStackPackage.get_quota_to_component_mapping()
        inv_map = {component_type: quota.name for quota, component_type in mapping.iteritems()}
        self.package.tenant.set_quota_usage(quota_name=inv_map[component_type], usage=usage)

        old_component = self.package.template.components.get(type=component_type)
        old_component.amount = old_limit
        old_component.save(update_fields=['amount'])

        new_component = self.new_template.components.get(type=component_type)
        new_component.amount = new_limit
        new_component.save(update_fields=['amount'])

    # Helper methods
    def get_valid_payload(self, template=None, package=None):
        return {
            'template': factories.PackageTemplateFactory.get_url(template or self.new_template),
            'package': factories.OpenStackPackageFactory.get_url(package or self.package),
        }


@ddt
class OpenStackPackageAssignTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.url = factories.OpenStackPackageFactory.get_list_url('assign')
        self.tenant = self.fixture.openstack_tenant
        self.template = factories.PackageTemplateFactory(service_settings=self.fixture.openstack_service_settings)

    def test_package_can_be_assigned_to_new_tenant(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            'tenant': openstack_factories.TenantFactory.get_url(self.tenant),
            'template': factories.PackageTemplateFactory.get_url(self.template)
        }
        self.assertFalse(models.OpenStackPackage.objects.filter(
            template=self.template,
            tenant=self.fixture.openstack_tenant,
        ).exists())

        response = self.client.post(self.url, payload)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        self.assertTrue(models.OpenStackPackage.objects.filter(
            template=self.template,
            tenant=self.fixture.openstack_tenant,
        ).exists())

    @data('owner', 'admin', 'manager')
    def test_user_cannot_assign_package(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            'tenant': openstack_factories.TenantFactory.get_url(self.tenant),
            'template': factories.PackageTemplateFactory.get_url(self.template)
        }

        response = self.client.post(self.url, payload)

        self.assertEquals(response.status_code, status.HTTP_403_FORBIDDEN)
