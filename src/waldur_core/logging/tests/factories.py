import factory
from django.contrib.contenttypes import models as ct_models
from django.urls import reverse

from waldur_core.logging import models
from waldur_core.logging.loggers import get_valid_events


class EventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Event

    message = factory.Sequence(lambda i: 'message#%s' % i)
    event_type = factory.Iterator(
        [
            'first_event',
            'second_event',
            'third_event',
            'fourth_event',
        ]
    )
    context = {
        'customer_abbreviation': 'TCAN',
        'customer_contact_details': 'test details',
        'customer_name': 'Test customer',
        'customer_uuid': 'test_customer_uuid',
        'host': 'example.com',
        'project_name': 'test_project',
        'project_uuid': 'test_project_uuid',
        'user_uuid': 'test_user_uuid',
    }

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('event-list')

    @classmethod
    def get_stats_list_url(cls):
        return 'http://testserver' + reverse('events-stats-list')


class FeedFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Feed

    event = factory.SubFactory(EventFactory)


class WebHookFactory(factory.django.DjangoModelFactory):
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
        return 'http://testserver' + reverse(
            'webhook-detail', kwargs={'uuid': hook.uuid.hex}
        )


class SystemNotificationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SystemNotification

    event_types = get_valid_events()[:3]
    roles = ['admin']
    hook_content_type = factory.LazyAttribute(
        lambda o: ct_models.ContentType.objects.get_by_natural_key(
            'logging', 'emailhook'
        )
    )
