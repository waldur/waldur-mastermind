import factory

from rest_framework.reverse import reverse

from nodeconductor_assembly_waldur.packages import models


class PackageTemplateFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PackageTemplate

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


class PackageComponentFactory(factory.django.DjangoModelFactory):
    class Meta(object):
        model = models.PackageComponent

    type = models.PackageComponent.Types.RAM
    template = factory.SubFactory(PackageTemplateFactory)
