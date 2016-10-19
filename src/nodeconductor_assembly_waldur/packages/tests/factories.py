import factory
from rest_framework.reverse import reverse

from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor.structure.tests.helpers import test_data
from nodeconductor_openstack import models as openstack_models, apps as openstack_apps

from .. import models


class PackageTemplateFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PackageTemplate

    service_settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'PackageTemplate%s' % n)

    @classmethod
    def get_url(cls, package_template=None, action=None):
        if package_template is None:
            package_template = PackageTemplateFactory()
        url = 'http://testserver' + reverse('package-template-detail', kwargs={'uuid': package_template.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(self):
        return 'http://testserver' + reverse('package-template-list')

    @factory.post_generation
    def components(self, create, extracted, **kwargs):
        if not create:
            return

        if extracted:
            for component in extracted:
                self.components.add(component)
        else:
            for component_type in self.get_required_component_types():
                self.components.get_or_create(type=component_type)


class PackageComponentFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PackageComponent

    type = models.PackageComponent.Types.RAM
    template = factory.SubFactory(PackageTemplateFactory)


# We do not have access to openstack factories so we need to build tenant manually.
def get_test_data_openstack_spl():
    service_settings = structure_factories.ServiceSettingsFactory(type=openstack_apps.OpenStackConfig.service_name)
    service = openstack_models.OpenStackService.objects.create(
        customer=test_data.customer, settings=service_settings, name=service_settings.name)
    return openstack_models.OpenStackServiceProjectLink.objects.create(project=test_data.project, service=service)


# We do not have access to openstack factories so we need to build tenant manually.
def get_test_data_tenant(**kwargs):
    spl = get_test_data_openstack_spl()
    return openstack_models.Tenant.objects.create(service_project_link=spl, **kwargs)


class OpenStackPackageFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.OpenStackPackage

    tenant = factory.Sequence(lambda n: get_test_data_tenant(name='tenant%s' % n))
    service_settings = factory.LazyAttribute(
        lambda package: structure_factories.ServiceSettingsFactory(type=openstack_apps.OpenStackConfig.service_name,
                                                                   scope=package.tenant)
    )
    template = factory.SubFactory(PackageTemplateFactory)

    @classmethod
    def get_url(cls, openstack_package=None, action=None):
        if openstack_package is None:
            openstack_package = OpenStackPackageFactory()
        url = 'http://testserver' + reverse('openstack-package-detail', kwargs={'uuid': openstack_package.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(self):
        return 'http://testserver' + reverse('openstack-package-list')
