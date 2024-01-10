import factory
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class ProjectEstimatedCostPolicyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.ProjectEstimatedCostPolicy

    project = factory.SubFactory(structure_factories.ProjectFactory)
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
