import factory
from rest_framework.reverse import reverse

from waldur_core.core import models as core_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import models


class GoogleCredentialsFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.GoogleCredentials

    service_provider = factory.SubFactory(marketplace_factories.ServiceProviderFactory)
    calendar_token = factory.Sequence(lambda n: "calendar_token_%s" % n)
    calendar_refresh_token = factory.Sequence(lambda n: "calendar_refresh_token_%s" % n)

    @classmethod
    def get_authorize_url(cls, credentials=None):
        if credentials is None:
            credentials = GoogleCredentialsFactory()

        return (
            "http://testserver"
            + reverse(
                "google-auth-detail",
                kwargs={"uuid": credentials.service_provider.uuid.hex},
            )
            + "authorize/"
        )

    @classmethod
    def get_url(cls, credentials=None):
        if credentials is None:
            credentials = GoogleCredentialsFactory()
        return "http://testserver" + reverse(
            "google-auth-detail",
            kwargs={"uuid": credentials.service_provider.uuid.hex},
        )

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("google-auth-list")
        return url if action is None else url + action + "/"


class GoogleCalendarFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.GoogleCalendar

    offering = factory.SubFactory(marketplace_factories.OfferingFactory)
    backend_id = factory.Sequence(lambda n: "%s@group.calendar.google.com" % n)
    state = core_models.StateMixin.States.OK
