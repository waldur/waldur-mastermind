import datetime

import factory
from django.urls import reverse

from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import models


class CampaignFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Campaign

    name = factory.Sequence(lambda n: "Campaign %s" % n)
    start_date = datetime.date.today()
    end_date = datetime.date.today() + datetime.timedelta(days=30)
    discount = 50
    discount_type = models.DiscountType.DISCOUNT
    service_provider = factory.SubFactory(marketplace_factories.ServiceProviderFactory)

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("promotions-campaign-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, campaign=None, action=None):
        if campaign is None:
            campaign = CampaignFactory()
        url = "http://testserver" + reverse(
            "promotions-campaign-detail", kwargs={"uuid": campaign.uuid.hex}
        )
        return url if action is None else url + action + "/"


class DiscountedResourceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.DiscountedResource

    campaign = factory.SubFactory(CampaignFactory)
    resource = factory.SubFactory(marketplace_factories.ResourceFactory)
