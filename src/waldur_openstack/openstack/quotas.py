from waldur_core.quotas import fields as quota_fields
from waldur_core.structure import models as structure_models

from . import models


TENANT_QUOTAS = (
    ('vpc_cpu_count', 'vcpu'),
    ('vpc_ram_size', 'ram'),
    ('vpc_storage_size', 'storage'),
    ('vpc_floating_ip_count', 'floating_ip_count'),
    ('vpc_instance_count', 'instances'),
)

TENANT_PATHS = {
    structure_models.Project: models.Tenant.Permissions.project_path,
    structure_models.Customer: models.Tenant.Permissions.customer_path,
}


def inject_tenant_quotas():
    for model in TENANT_PATHS:
        for (quota_name, child_quota_name) in TENANT_QUOTAS:

            def get_children(scope):
                path = TENANT_PATHS[type(scope)]
                return models.Tenant.objects.filter(**{path: scope})

            model.add_quota_field(
                name=quota_name,
                quota_field=quota_fields.UsageAggregatorQuotaField(
                    get_children=get_children,
                    child_quota_name=child_quota_name,
                )
            )
