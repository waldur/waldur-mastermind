from __future__ import unicode_literals

from random import randint
import uuid

from django.urls import reverse
import factory

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class UrlModelFactory(factory.DjangoModelFactory):
    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = ServiceFactory()

        kwargs = {}
        if hasattr(service, 'uuid'):
            kwargs['uuid'] = service.uuid
        else:
            kwargs['pk'] = service.pk
        url = 'http://testserver' + reverse('{}-detail'.format(cls._meta.model.get_url_name()), kwargs=kwargs)
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('{}-list'.format(cls._meta.model.get_url_name()))


class ServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = 'Rijkscloud'
    username = 'admin'
    token = 'secret'


class ServiceFactory(UrlModelFactory):
    class Meta(object):
        model = models.RijkscloudService

    settings = factory.SubFactory(ServiceSettingsFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class ServiceProjectLinkFactory(UrlModelFactory):
    class Meta(object):
        model = models.RijkscloudServiceProjectLink

    service = factory.SubFactory(ServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)


class FlavorFactory(UrlModelFactory):
    class Meta(object):
        model = models.Flavor

    name = factory.Sequence(lambda n: 'flavor%s' % n)
    settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)

    cores = 2
    ram = 2 * 1024

    backend_id = factory.Sequence(lambda n: 'flavor-id%s' % n)


class VolumeFactory(UrlModelFactory):
    class Meta(object):
        model = models.Volume

    name = factory.Sequence(lambda n: 'volume%s' % n)
    service_project_link = factory.SubFactory(ServiceProjectLinkFactory)
    size = 10 * 1024
    backend_id = factory.LazyAttribute(lambda _: str(uuid.uuid4()))


class FloatingIPFactory(UrlModelFactory):
    class Meta(object):
        model = models.FloatingIP

    name = factory.Sequence(lambda n: 'floating_ip%s' % n)
    settings = factory.SubFactory(ServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: 'backend_id_%s' % n)
    address = factory.LazyAttribute(lambda o: '.'.join('%s' % randint(0, 255) for _ in range(4)))


class NetworkFactory(UrlModelFactory):
    class Meta(object):
        model = models.Network

    name = factory.Sequence(lambda n: 'network_%s' % n)
    settings = factory.SubFactory(ServiceSettingsFactory)


class SubNetFactory(UrlModelFactory):
    class Meta(object):
        model = models.SubNet

    name = factory.Sequence(lambda n: 'subnet%s' % n)
    backend_id = factory.Sequence(lambda n: 'backend_id_%s' % n)
    settings = factory.SubFactory(ServiceSettingsFactory)
    network = factory.SubFactory(NetworkFactory)
    gateway_ip = factory.LazyAttribute(lambda o: '.'.join('%s' % randint(0, 255) for _ in range(4)))


class InternalIPFactory(UrlModelFactory):
    class Meta(object):
        model = models.InternalIP

    name = factory.Sequence(lambda n: 'internal_ip%s' % n)
    settings = factory.SubFactory(ServiceSettingsFactory)
    subnet = factory.SubFactory(SubNetFactory)
    backend_id = factory.Sequence(lambda n: 'backend_id_%s' % n)
    address = factory.LazyAttribute(lambda o: '.'.join('%s' % randint(0, 255) for _ in range(4)))


class InstanceFactory(UrlModelFactory):
    class Meta(object):
        model = models.Instance

    name = factory.Sequence(lambda n: 'vm_%s' % n)
    flavor_name = factory.Sequence(lambda n: 'flavor.m%s' % n)
    service_project_link = factory.SubFactory(ServiceProjectLinkFactory)
    backend_id = factory.Sequence(lambda n: 'vm_%s' % n)
    internal_ip = factory.SubFactory(InternalIPFactory)
    floating_ip = factory.SubFactory(FloatingIPFactory)
