import factory
from rest_framework.reverse import reverse

from nodeconductor.structure.tests import factories as structure_factories

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


class OpenStackPackageFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.OpenStackPackage

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
