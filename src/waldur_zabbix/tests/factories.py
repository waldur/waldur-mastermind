import factory

from django.core.urlresolvers import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models
from ..apps import ZabbixConfig


class ServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = ZabbixConfig.service_name


class ZabbixServiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.ZabbixService

    settings = factory.SubFactory(ServiceSettingsFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = ZabbixServiceFactory()
        url = 'http://testserver' + reverse('zabbix-detail', kwargs={'uuid': service.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('zabbix-list')


class ZabbixServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.ZabbixServiceProjectLink

    service = factory.SubFactory(ZabbixServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, spl=None):
        if spl is None:
            spl = ZabbixServiceProjectLinkFactory()
        return 'http://testserver' + reverse('zabbix-spl-detail', kwargs={'pk': spl.pk})


class HostFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Host

    service_project_link = factory.SubFactory(ZabbixServiceProjectLinkFactory)
    name = factory.Sequence(lambda n: 'host%s' % n)
    backend_id = factory.Sequence(lambda n: 'host-id%s' % n)

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('zabbix-host-list')

    @classmethod
    def get_url(cls, host=None, action=None):
        if host is None:
            host = HostFactory()
        url = 'http://testserver' + reverse('zabbix-host-detail', kwargs={'uuid': host.uuid.hex})
        return url if action is None else url + action + '/'


class ITServiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.ITService

    service_project_link = factory.SubFactory(ZabbixServiceProjectLinkFactory)
    host = factory.SubFactory(HostFactory)
    name = factory.Sequence(lambda n: 'itservice%s' % n)
    backend_id = factory.Sequence(lambda n: 'itservice-id%s' % n)

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = ITServiceFactory()
        url = 'http://testserver' + reverse('zabbix-itservice-detail', kwargs={'uuid': service.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_events_url(cls, service):
        return cls.get_url(service, 'events')


class TemplateFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Template

    name = factory.Sequence(lambda n: 'zabbix-template#%s' % n)
    settings = factory.SubFactory(ServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: 'zabbix-template-id%s' % n)

    @classmethod
    def get_url(cls, template=None, action=None):
        if template is None:
            template = TemplateFactory()
        url = 'http://testserver' + reverse('zabbix-template-detail', kwargs={'uuid': template.uuid})
        return url if action is None else url + action + '/'
