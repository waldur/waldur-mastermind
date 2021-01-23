import random

import factory

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.packages import models
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests.factories import TenantFactory


class PackageTemplateFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.PackageTemplate

    service_settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'PackageTemplate%s' % n)
    archived = False

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
                self.components.get_or_create(
                    type=component_type,
                    price=random.randint(1, 2),  # noqa: S311
                    amount=1,
                )


# XXX: this factory is useless. On template creation its components are already
# generated in 'post_generation.components' method. So it is impossible to add
# any new component to it.
class PackageComponentFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.PackageComponent

    type = models.PackageComponent.Types.RAM
    template = factory.SubFactory(PackageTemplateFactory)
    price = factory.fuzzy.FuzzyInteger(10, 20)


class OpenStackServiceFactory(factory.DjangoModelFactory):
    class Meta:
        model = openstack_models.OpenStackService

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)


class OpenStackPackageFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.OpenStackPackage

    tenant = factory.SubFactory(TenantFactory)
    template = factory.SubFactory(PackageTemplateFactory)
    service_settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)
