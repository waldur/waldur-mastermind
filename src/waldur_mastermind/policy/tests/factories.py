import factory
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import models


class ProjectEstimatedCostPolicyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.ProjectEstimatedCostPolicy

    scope = factory.SubFactory(structure_factories.ProjectFactory)
    limit_cost = 10
    actions = "notify_project_team,block_creation_of_new_resources"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse(
            "marketplace-project-estimated-cost-policy-list"
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, policy=None, action=None):
        if policy is None:
            policy = ProjectEstimatedCostPolicyFactory()
        url = "http://testserver" + reverse(
            "marketplace-project-estimated-cost-policy-detail",
            kwargs={"uuid": policy.uuid.hex},
        )
        return url if action is None else url + action + "/"


class CustomerEstimatedCostPolicyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.CustomerEstimatedCostPolicy

    scope = factory.SubFactory(structure_factories.CustomerFactory)
    limit_cost = 10
    actions = "notify_organization_owners,block_creation_of_new_resources"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse(
            "marketplace-customer-estimated-cost-policy-list"
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, policy=None, action=None):
        if policy is None:
            policy = CustomerEstimatedCostPolicyFactory()
        url = "http://testserver" + reverse(
            "marketplace-customer-estimated-cost-policy-detail",
            kwargs={"uuid": policy.uuid.hex},
        )
        return url if action is None else url + action + "/"


class OfferingEstimatedCostPolicyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingEstimatedCostPolicy

    scope = factory.SubFactory(marketplace_factories.OfferingFactory)
    limit_cost = 10
    actions = "notify_organization_owners,block_creation_of_new_resources"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse(
            "marketplace-offering-estimated-cost-policy-list"
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, policy=None, action=None):
        if policy is None:
            policy = CustomerEstimatedCostPolicyFactory()
        url = "http://testserver" + reverse(
            "marketplace-offering-estimated-cost-policy-detail",
            kwargs={"uuid": policy.uuid.hex},
        )
        return url if action is None else url + action + "/"


class OfferingUsagePolicyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingUsagePolicy

    scope = factory.SubFactory(marketplace_factories.OfferingFactory)
    actions = "notify_organization_owners,block_creation_of_new_resources"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-offering-usage-policy-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, policy=None, action=None):
        if policy is None:
            policy = CustomerEstimatedCostPolicyFactory()
        url = "http://testserver" + reverse(
            "marketplace-offering-usage-policy-detail",
            kwargs={"uuid": policy.uuid.hex},
        )
        return url if action is None else url + action + "/"


class OfferingUsageComponentLimitFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingComponentLimit

    policy = factory.SubFactory(OfferingUsagePolicyFactory)
    limit = 10
    component = factory.SubFactory(marketplace_factories.OfferingFactory)
