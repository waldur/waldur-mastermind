from uuid import uuid4

import factory
from django.utils import timezone
from rest_framework.reverse import reverse

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_remote import models


class RemoteSynchronisationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.RemoteSynchronisation],
):
    class Meta:
        model = models.RemoteSynchronisation

    api_url = "http://example.com"
    token = factory.LazyFunction(lambda: uuid4().hex)
    remote_organization_uuid = factory.LazyFunction(uuid4)
    remote_organization_name = factory.Sequence(
        lambda n: "remote_organization_name-%s" % n
    )
    local_service_provider = factory.SubFactory(
        marketplace_factories.ServiceProviderFactory
    )
    last_execution = factory.LazyFunction(timezone.now)

    @classmethod
    def get_url(cls, synchronisation=None, action=None):
        if synchronisation is None:
            synchronisation = RemoteSynchronisationFactory()
        url = "http://testserver" + reverse(
            "marketplace-remote-synchronisation-detail",
            kwargs={"uuid": synchronisation.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-remote-synchronisation-list")
        return url if action is None else url + action + "/"


class RemoteLocalCategoryFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.RemoteLocalCategory],
):
    class Meta:
        model = models.RemoteLocalCategory

    local_category = factory.SubFactory(marketplace_factories.CategoryFactory)
    remote_category = factory.LazyFunction(uuid4)
    remote_synchronisation = factory.SubFactory(RemoteSynchronisationFactory)
