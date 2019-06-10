import factory
import random

from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests.factories import TenantFactory

from .. import models


class PackageTemplateFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PackageTemplate

    service_settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'PackageTemplate%s' % n)
    archived = False

    @classmethod
    def get_url(cls, package_template=None, action=None):
        if package_template is None:
            package_template = PackageTemplateFactory()
        url = 'http://testserver' + reverse('package-template-detail', kwargs={'uuid': package_template.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('package-template-list')

    @factory.post_generation
    def components(self, create, extracted, **kwargs):
        if not create:
            return

        if extracted is not None:
            for component in extracted:
                component.template = self
                component.save()
        else:
            for component_type in self.get_required_component_types():
                self.components.get_or_create(type=component_type, price=random.randint(1, 2), amount=1)


# XXX: this factory is useless. On template creation its components are already
# generated in 'post_generation.components' method. So it is impossible to add
# any new component to it.
class PackageComponentFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PackageComponent

    type = models.PackageComponent.Types.RAM
    template = factory.SubFactory(PackageTemplateFactory)
    price = factory.fuzzy.FuzzyInteger(10, 20)


class OpenStackServiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = openstack_models.OpenStackService

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)


class OpenStackServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = openstack_models.OpenStackServiceProjectLink

    service = factory.SubFactory(OpenStackServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, service_project_link=None, action=None):
        if service_project_link is None:
            service_project_link = OpenStackServiceProjectLinkFactory()
        url = 'http://testserver' + reverse('openstack-spl-detail', kwargs={'pk': service_project_link.pk})
        return url if action is None else url + action + '/'


class OpenStackPackageFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.OpenStackPackage

    tenant = factory.SubFactory(TenantFactory)
    template = factory.SubFactory(PackageTemplateFactory)
    service_settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)

    @classmethod
    def get_url(cls, openstack_package=None, action=None):
        if openstack_package is None:
            openstack_package = OpenStackPackageFactory()
        url = 'http://testserver' + reverse('openstack-package-detail', kwargs={'uuid': openstack_package.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('openstack-package-list')
        return url if action is None else url + action + '/'
