from __future__ import unicode_literals

from django.urls import reverse
import factory

from waldur_core.logging import models
# Dependency from `structure` application exists only in tests
from waldur_core.logging.loggers import get_valid_events
from waldur_core.structure.tests import factories as structure_factories


class EventFactory(object):
    """
    Event factory that provides default data for events.

    Created event fields can be accessible via .fields attribute of created event.
    """

    def __init__(self, **kwargs):
        self.create(**kwargs)
        self.save()

    def create(self, **kwargs):
        """
        Creates event fields values.

        If field is in kwargs - value from kwargs will be used for this field,
        otherwise - default value will be used for field.
        """
        self.fields = {
            '@timestamp': '2015-04-19T16:25:45.376+04:00',
            '@version': 1,
            'customer_abbreviation': 'TCAN',
            'customer_contact_details': 'test details',
            'customer_name': 'Test cusomter',
            'customer_uuid': 'test_customer_uuid',
            'event_type': 'test_event_type',
            'host': 'example.com',
            'importance': 'high',
            'importance_code': 30,
            'levelname': 'WARNING',
            'logger': 'waldur_core.test',
            'message': 'Test message',
            'project_name': 'test_project',
            'project_uuid': 'test_project_uuid',
            'tags': ['_jsonparsefailure'],
            'type': 'gcloud-event',
            'user_uuid': 'test_user_uuid',
        }
        for key, value in kwargs.items():
            self.fields[key] = value

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('event-list')


class AlertFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Alert

    message = factory.Sequence(lambda i: 'message#%s' % i)
    alert_type = factory.Iterator(['first_alert', 'second_alert', 'third_alert', 'fourth_alert'])
    severity = factory.Iterator([
        models.Alert.SeverityChoices.DEBUG, models.Alert.SeverityChoices.INFO,
        models.Alert.SeverityChoices.WARNING, models.Alert.SeverityChoices.ERROR])
    context = {'test': 'test'}
    scope = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('alert-list')

    @classmethod
    def get_url(cls, alert=None, action=None):
        if alert is None:
            alert = AlertFactory()
        url = 'http://testserver' + reverse('alert-detail', kwargs={'uuid': alert.uuid})
        return url if action is None else url + action + '/'


class WebHookFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.WebHook

    event_types = get_valid_events()[:3]
    destination_url = 'http://example.com/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('webhook-list')

    @classmethod
    def get_url(cls, hook=None):
        if hook is None:
            hook = WebHookFactory()
        return 'http://testserver' + reverse('webhook-detail', kwargs={'uuid': hook.uuid})


class PushHookFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.PushHook

    event_types = get_valid_events()[:3]
    token = 'VALID_TOKEN'
    type = models.PushHook.Type.ANDROID

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('pushhook-list')

    @classmethod
    def get_url(cls, hook=None):
        if hook is None:
            hook = PushHookFactory()
        return 'http://testserver' + reverse('pushhook-detail', kwargs={'uuid': hook.uuid})
