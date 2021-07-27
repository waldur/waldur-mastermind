import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class NotificationFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Notification

    author = factory.SubFactory(structure_factories.UserFactory)
    subject = factory.Sequence(lambda n: 'subject-%s' % n)
    body = factory.Sequence(lambda n: 'body-%s' % n)

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('notification-list')
        return url if action is None else url + action + '/'
