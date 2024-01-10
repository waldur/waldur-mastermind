import factory
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_flows import models


class OfferingStateRequestFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingStateRequest

    offering = factory.SubFactory(
        marketplace_factories.OfferingFactory,
        state=marketplace_models.Offering.States.DRAFT,
    )
    requested_by = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, offering_request=None, action=None):
        if offering_request is None:
            offering_request = OfferingStateRequestFactory()
        url = "http://testserver" + reverse(
            "marketplace-offering-activate-request-detail",
            kwargs={"uuid": offering_request.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse(
            "marketplace-offering-activate-request-list"
        )
        return url if action is None else url + action + "/"
