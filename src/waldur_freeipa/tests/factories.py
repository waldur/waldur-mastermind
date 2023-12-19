import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_freeipa import models


class ProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Profile

    username = factory.Sequence(lambda n: 'john%s' % n)
    user = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('freeipa-profile-list')

    @classmethod
    def get_url(cls, user, action=None):
        url = 'http://testserver' + reverse(
            'freeipa-profile-detail', kwargs={'uuid': user.uuid.hex}
        )
        return url if action is None else url + action + '/'
