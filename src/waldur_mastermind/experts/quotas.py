from django.db.models import Q

from waldur_core.quotas import fields as quota_fields
from waldur_core.structure import models as structure_models

from . import models


QUOTA_NAME = 'nc_expert_count'


def add_quota_field():
    structure_models.Project.add_quota_field(
        name=QUOTA_NAME,
        quota_field=quota_fields.QuotaField()
    )
    structure_models.Customer.add_quota_field(
        name=QUOTA_NAME,
        quota_field=quota_fields.QuotaField()
    )


def update_project_quota(project):
    if not project:
        return
    valid_states = (models.ExpertRequest.States.ACTIVE, models.ExpertRequest.States.PENDING)
    query = Q(project=project, state__in=valid_states)
    count = models.ExpertRequest.objects.filter(query).count()
    project.set_quota_usage(QUOTA_NAME, count)


def update_customer_quota(customer):
    if not customer:
        return
    valid_states = (models.ExpertRequest.States.ACTIVE, models.ExpertRequest.States.PENDING)
    query = Q(contract__team__customer=customer, state__in=valid_states)
    count = models.ExpertRequest.objects.filter(query).count()
    customer.set_quota_usage(QUOTA_NAME, count)


def get_request_customer(request):
    try:
        return request.contract.team.customer
    except AttributeError:
        return None


def get_contract_customer(contract):
    try:
        return contract.team.customer
    except AttributeError:
        return None


def get_experts_count(scope):
    return scope.quotas.get(name=QUOTA_NAME).usage
