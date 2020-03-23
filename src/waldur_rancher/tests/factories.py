import factory
from django.urls import reverse

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories

from .. import models


class RancherServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    class Meta:
        model = structure_models.ServiceSettings

    type = 'Rancher'
    backend_url = 'https://example.com'
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class RancherServiceFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.RancherService

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    customer = factory.SelfAttribute('settings.customer')

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = RancherServiceFactory()
        url = 'http://testserver' + reverse(
            'rancher-detail', kwargs={'uuid': service.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-list')


class RancherServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.RancherServiceProjectLink

    service = factory.SubFactory(RancherServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, spl=None, action=None):
        if spl is None:
            spl = RancherServiceProjectLinkFactory()
        url = 'http://testserver' + reverse('rancher-spl-detail', kwargs={'pk': spl.pk})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-spl-list')


class ClusterFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Cluster

    service_project_link = factory.SubFactory(RancherServiceProjectLinkFactory)
    name = factory.Sequence(lambda n: 'cluster-%s' % n)
    backend_id = factory.Sequence(lambda n: 'cluster-%s' % n)

    @classmethod
    def get_url(cls, cluster=None, action=None):
        cluster = cluster or ClusterFactory()
        url = 'http://testserver' + reverse(
            'rancher-cluster-detail', kwargs={'uuid': cluster.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('rancher-cluster-list')
        return url if action is None else url + action + '/'


class NodeFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Node

    cluster = factory.SubFactory(ClusterFactory)
    backend_id = factory.Sequence(lambda n: 'node-%s' % n)

    @classmethod
    def get_url(cls, node=None, action=None):
        node = node or NodeFactory()
        url = 'http://testserver' + reverse(
            'rancher-node-detail', kwargs={'uuid': node.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-node-list')


class RancherUserFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.RancherUser

    user = factory.SubFactory(structure_factories.UserFactory)
    settings = factory.SubFactory(RancherServiceSettingsFactory)


class CatalogFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Catalog

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: 'node-%s' % n)

    @classmethod
    def get_url(cls, catalog=None, action=None):
        catalog = catalog or CatalogFactory()
        url = 'http://testserver' + reverse(
            'rancher-catalog-detail', kwargs={'uuid': catalog.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-catalog-list')


class TemplateFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Template

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: 'node-%s' % n)
    versions = []

    @classmethod
    def get_url(cls, template=None, action=None):
        template = template or TemplateFactory()
        url = 'http://testserver' + reverse(
            'rancher-template-detail', kwargs={'uuid': template.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-template-list')


class ProjectFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Project

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: 'node-%s' % n)

    @classmethod
    def get_url(cls, project=None, action=None):
        project = project or ProjectFactory()
        url = 'http://testserver' + reverse(
            'rancher-project-detail', kwargs={'uuid': project.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-project-list')


class NamespaceFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Namespace

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: 'node-%s' % n)

    @classmethod
    def get_url(cls, namespace=None, action=None):
        namespace = namespace or NamespaceFactory()
        url = 'http://testserver' + reverse(
            'rancher-namespace-detail', kwargs={'uuid': namespace.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-namespace-list')
