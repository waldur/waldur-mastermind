import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_zabbix import models

from ..apps import ZabbixConfig


class ZabbixServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = ZabbixConfig.service_name


class HostFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Host

    service_settings = factory.SubFactory(ZabbixServiceSettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    name = factory.Sequence(lambda n: 'host%s' % n)
    backend_id = factory.Sequence(lambda n: 'host-id%s' % n)

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('zabbix-host-list')

    @classmethod
    def get_url(cls, host=None, action=None):
        if host is None:
            host = HostFactory()
        url = 'http://testserver' + reverse(
            'zabbix-host-detail', kwargs={'uuid': host.uuid.hex}
        )
        return url if action is None else url + action + '/'


class ITServiceFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.ITService

    service_settings = factory.SubFactory(ZabbixServiceSettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    host = factory.SubFactory(HostFactory)
    name = factory.Sequence(lambda n: 'itservice%s' % n)
    backend_id = factory.Sequence(lambda n: 'itservice-id%s' % n)

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = ITServiceFactory()
        url = 'http://testserver' + reverse(
            'zabbix-itservice-detail', kwargs={'uuid': service.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_events_url(cls, service):
        return cls.get_url(service, 'events')


class TemplateFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Template

    name = factory.Sequence(lambda n: 'zabbix-template#%s' % n)
    settings = factory.SubFactory(ZabbixServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: 'zabbix-template-id%s' % n)

    @classmethod
    def get_url(cls, template=None, action=None):
        if template is None:
            template = TemplateFactory()
        url = 'http://testserver' + reverse(
            'zabbix-template-detail', kwargs={'uuid': template.uuid.hex}
        )
        return url if action is None else url + action + '/'
