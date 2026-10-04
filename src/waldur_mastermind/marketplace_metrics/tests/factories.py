import factory
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import enums, models


class MetricDefinitionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.MetricDefinition

    key = factory.Sequence(lambda n: "education.course.completions_%s" % n)
    name = "Course completions"
    unit = "{learners}"
    kind = enums.MetricKinds.COUNTER
    attribute_keys = factory.LazyFunction(lambda: ["course"])

    @classmethod
    def get_url(cls, definition=None):
        definition = definition or cls()
        return reverse(
            "marketplace-metric-definition-detail", kwargs={"uuid": definition.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return reverse("marketplace-metric-definition-list")


class OfferingMetricFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingMetric

    offering = factory.SubFactory(marketplace_factories.OfferingFactory)
    definition = factory.SubFactory(MetricDefinitionFactory)

    @classmethod
    def get_url(cls, offering_metric=None, action=None):
        offering_metric = offering_metric or cls()
        url = reverse(
            "marketplace-offering-metric-detail",
            kwargs={"uuid": offering_metric.uuid.hex},
        )
        return url + action + "/" if action else url

    @classmethod
    def get_list_url(cls):
        return reverse("marketplace-offering-metric-list")


class MetricSeriesFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.MetricSeries

    resource = factory.SubFactory(marketplace_factories.ResourceFactory)
    offering_metric = factory.SubFactory(OfferingMetricFactory)
    attributes = factory.LazyFunction(dict)
    attributes_hash = factory.Sequence(lambda n: "%064d" % n)
