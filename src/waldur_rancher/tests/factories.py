from django.urls import reverse

import factory

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure import models as structure_models

from .. import models


class RancherServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    class Meta(object):
        model = structure_models.ServiceSettings

    type = 'Rancher'
    backend_url = 'https://example.com'
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class RancherServiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.RancherService

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    customer = factory.SelfAttribute('settings.customer')

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = RancherServiceFactory()
        url = 'http://testserver' + reverse('rancher-detail', kwargs={'uuid': service.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-list')


class RancherServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta(object):
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
    class Meta(object):
        model = models.Cluster

    service_project_link = factory.SubFactory(RancherServiceProjectLinkFactory)
    name = factory.Sequence(lambda n: 'cluster-%s' % n)
    backend_id = factory.Sequence(lambda n: 'cluster-%s' % n)

    @classmethod
    def get_url(cls, cluster=None, action=None):
        cluster = cluster or ClusterFactory()
        url = 'http://testserver' + reverse('rancher-cluster-detail', kwargs={'uuid': cluster.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-cluster-list')


class NodeFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Node

    @classmethod
    def get_url(cls, node, action=None):
        url = 'http://testserver' + reverse('rancher-node-detail', kwargs={'uuid': node.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('rancher-node-list')
