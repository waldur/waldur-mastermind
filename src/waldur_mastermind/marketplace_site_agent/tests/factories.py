import factory
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace.tests.factories import OfferingFactory
from waldur_mastermind.marketplace_site_agent import models


class AgentIdentityFactory(factory.django.DjangoModelFactory):
    offering = factory.SubFactory(OfferingFactory)
    name = factory.Sequence(lambda n: f"Agent {n}")

    class Meta:
        model = models.AgentIdentity

    @classmethod
    def get_url(cls, identity=None, action=None):
        if identity is None:
            identity = AgentIdentityFactory()
        url = "http://testserver" + reverse(
            "marketplace-site-agent-identity-detail", kwargs={"uuid": identity.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-site-agent-identity-list")
        return url if action is None else url + action + "/"


class AgentServiceFactory(factory.django.DjangoModelFactory):
    identity = factory.SubFactory(AgentIdentityFactory)
    name = factory.Sequence(lambda n: f"Service {n}")
    mode = "event_processing"

    class Meta:
        model = models.AgentService

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = AgentServiceFactory()
        url = "http://testserver" + reverse(
            "marketplace-site-agent-service-detail", kwargs={"uuid": service.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-site-agent-service-list")
        return url if action is None else url + action + "/"


class AgentProcessorFactory(factory.django.DjangoModelFactory):
    service = factory.SubFactory(AgentServiceFactory)
    name = factory.Sequence(lambda n: f"Processor {n}")
    backend_type = "SLURM"

    class Meta:
        model = models.AgentProcessor

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-site-agent-processor-list")
        return url if action is None else url + action + "/"


class SiteAgentLogFactory(factory.django.DjangoModelFactory):
    agent_identity = factory.SubFactory(AgentIdentityFactory)
    timestamp = factory.LazyFunction(lambda: 1_746_355_200.0)
    level = "INFO"
    message = factory.Sequence(lambda n: f"Log message {n}")
    module = "waldur_site_agent.test"

    class Meta:
        model = models.SiteAgentLog

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("marketplace-site-agent-log-list")
